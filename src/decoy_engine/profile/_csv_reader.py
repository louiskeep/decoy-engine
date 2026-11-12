"""The one reader for CSV sources.

`read_csv_source` (public) and `CsvFileSource` (profile, validate, compile) both read through
`_read_csv_rows`, so there is one copy of the headerless width scan in the engine.

Pattern: pandas cannot fail closed on a headerless file whose rows disagree with the
declared `column_names`. A short row is padded and a long row is dropped or shifts. Frame
width alone does not catch it, because pandas normalizes every row to the widest one. So a
headerless read first checks the field count of each consumed record with the stdlib `csv`
parser, then reads positionally and assigns the declared names.

The scan agrees with pandas on line boundaries and quoting for every file it admits:

- `utf-8-sig` strips the BOM pandas strips, and the stdlib field-size cap is lifted (below).
- A line is skipped only when it is empty or one whitespace-only field with no delimiter,
  which is what pandas' `skip_blank_lines` does.
- A NUL byte and a lone CR are refused outright. The two parsers treat them differently, so
  no sanitizing keeps them in agreement. Only LF and CRLF endings are reconcilable.
- They can still disagree within a line, for example an unterminated quote (`1,2,"3`), which
  `csv` tolerates and pandas rejects. The pandas read and the `shape[1]` backstop catch that
  and refuse with the same coded error, so no frame or misaligned value escapes.
- Blank-line limitation: a line whose only field is quoted-empty (`""`), quoted-whitespace or
  a non-breaking space counts as blank to the scan, while pandas keeps it as a one-field row.
  Because line boundaries agree, pandas pads it to a row whose only value sits in its own
  leading column. No real value is misaligned and no cell leaks.

Every refusal is a `PipelineValidationError` with a registered code, built by `source_refusal`:
`<code>: source <table> <detail>`. The detail names the line and the counts, never a cell
value, a path or a file name, and the library error is never chained.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import os
import stat
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, TextIO, overload

import pandas as pd

from decoy_engine import fileaccess
from decoy_engine._lexeme import pandas_lexeme_kwargs
from decoy_engine.config._csv_dialect import CSV_DIALECT_DEFAULTS, CsvDialect, csv_dialect_problem
from decoy_engine.errors import ConfigError, PipelineValidationError
from decoy_engine.fileaccess import FileAccess, FileHandle, FileRequest, FileRole
from decoy_engine.fileaccess.adapters import pandas as pandas_adapter
from decoy_engine.fileaccess.adapters.memory import MemoryHandle
from decoy_engine.validation_result import CODES

if TYPE_CHECKING:
    import _csv

# The scan tokenizes with the stdlib csv parser, which caps a field at 131072 characters;
# pandas has no such cap. Raise it to the platform maximum so a large text or JSON field is
# not falsely refused, narrowing on platforms where sys.maxsize overflows the C long.
_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(_limit)
        break
    except OverflowError:
        _limit //= 2

OpenHandle = Callable[[], FileHandle]
_PARSER_ERRORS = pandas_adapter.PARSER_ERRORS


# A header file holds one record of names; anything larger is not one.
_HEADER_FILE_MAX_BYTES = 1 << 20


@fileaccess.entry_point
def resolve_csv_dialect(
    descriptor: Mapping[str, Any],
    *,
    table: str = "source",
    file_access: FileAccess | None = None,
) -> CsvDialect:
    """The `CsvDialect` a validated source descriptor declares.

    The only place in the engine that reads the dialect keys of a descriptor. `has_header`
    must be present (`PipelineConfig` requires it for csv); without it this raises
    `ConfigError` before any IO. A `column_names_file` is read here, under the bound
    file-access policy, and gives the same dialect as the same names written inline.
    """
    has_header = descriptor.get("has_header")
    if not isinstance(has_header, bool):
        raise ConfigError(
            "resolve_csv_dialect: has_header must be true or false; "
            "validate the descriptor with PipelineConfig first"
        )
    names_file = descriptor.get("column_names_file")
    if names_file is None:
        # Only the keys present are passed, so `CsvDialect`'s field defaults are the one home
        # of the defaults and `CsvDialect` alone validates and normalizes the values.
        present = {
            f.name: descriptor[f.name]
            for f in dataclasses.fields(CsvDialect)
            if f.name in descriptor
        }
        return CsvDialect(**present)
    delimiter = descriptor.get("delimiter", CSV_DIALECT_DEFAULTS.delimiter)
    problem = csv_dialect_problem(
        has_header, descriptor.get("column_names"), delimiter, column_names_file=names_file
    )
    if problem is not None:
        raise ConfigError(f"CsvDialect: {problem}")
    names = resolve_column_names_file(names_file, delimiter=delimiter, table=table)
    return CsvDialect(has_header=False, column_names=names, delimiter=delimiter)


def resolve_column_names_file(path: str, *, delimiter: str, table: str) -> tuple[str, ...]:
    """The names in a one-record header file, read like a data file's header row.

    Runs under the policy `resolve_csv_dialect` bound. Names are not stripped. Every refusal
    is `column_names_file_invalid` with a fixed phrase, raised outside any `except` block.
    """
    failure: str | None = None
    data = b""
    try:
        data = fileaccess.read_checked(
            FileRole.SOURCE,
            path,
            f"sources.{table}.column_names_file",
            check=_header_file_check(table),
        )
    except OSError as exc:
        if fileaccess.keeps_class(exc):
            raise
        failure = "not found" if isinstance(exc, FileNotFoundError) else "not readable"
    if failure is None and len(data) > _HEADER_FILE_MAX_BYTES:
        failure = "too large"  # the file grew after the `fstat` check
    if failure is not None:
        raise _names_file_refusal(table, failure)
    records: list[list[str]] = []
    try:
        text = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline="")
        reader = _record_reader(
            text,
            delimiter,
            on_nul=lambda: _names_file_refusal(table, "NUL byte"),
            on_lone_cr=lambda: _names_file_refusal(table, "lone carriage return"),
        )
        records = [record for record in reader if not _is_blank_record(record)]
    except UnicodeDecodeError:
        failure = "not UTF-8 text"
    if failure is None and not records:
        failure = "empty"
    if failure is None and len(records) > 1:
        failure = "more than one record"
    if failure is not None:
        raise _names_file_refusal(table, failure)
    names = records[0]
    problem = csv_dialect_problem(False, names, delimiter)
    if problem is not None:
        # `csv_dialect_problem` decides; the position only says where.
        raise _names_file_refusal(table, _name_position(names) or "not readable")
    return tuple(names)


def _name_position(names: Sequence[str]) -> str | None:
    for position, name in enumerate(names, 1):
        if not name.strip():
            return f"blank name at position {position}"
    seen: dict[str, int] = {}
    for position, name in enumerate(names, 1):
        if name in seen:
            return f"duplicate name at positions {seen[name]} and {position}"
        seen[name] = position
    return None


def _header_file_check(table: str) -> Callable[[os.stat_result], None]:
    def check(info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise _names_file_refusal(table, "not readable")
        if info.st_size > _HEADER_FILE_MAX_BYTES:
            raise _names_file_refusal(table, "too large")

    return check


def _names_file_refusal(table: str, phrase: str) -> PipelineValidationError:
    return source_refusal(
        CODES.SOURCE_FILE_COLUMN_NAMES_FILE_INVALID,
        table,
        f"column_names_file is not a one-record header file ({phrase})",
    )


def source_refusal(code: str, table: str, detail: str) -> PipelineValidationError:
    """The one constructor of this module's refusals: coded, names the table, no value."""
    return PipelineValidationError(f"{code}: source {table} {detail}", code=code)


def _is_blank_record(record: Sequence[str]) -> bool:
    """A record pandas' `skip_blank_lines` skips: empty, or one whitespace-only field."""
    return len(record) <= 1 and (not record or not record[0].strip())


def _record_reader(
    text: TextIO,
    delimiter: str,
    *,
    on_nul: Callable[[], PipelineValidationError],
    on_lone_cr: Callable[[], PipelineValidationError],
) -> _csv.Reader:
    """The module's one `csv.reader`: it refuses a NUL byte and a lone CR through the caller's
    own refusals, raised directly so nothing is chained."""

    def _reject_unreconcilable(lines: Iterable[str]) -> Iterator[str]:
        # With newline="" the wrapper splits on CR, LF and CRLF, so a lone-CR ending
        # leaves a trailing "\r" and CRLF a trailing "\r\n". Strip one LF or CRLF; any
        # "\r" left is a lone CR.
        for line in lines:
            if "\x00" in line:
                raise on_nul()
            body = line[:-2] if line.endswith("\r\n") else line.rstrip("\n")
            if "\r" in body:
                raise on_lone_cr()
            yield line

    return csv.reader(_reject_unreconcilable(text), delimiter=delimiter)


def _positional_lexemes(columns: tuple[str, ...], names: list[str]) -> dict[str, dict[int, type]]:
    """Headerless files key converters by position, so map the declared names onto it."""
    return pandas_lexeme_kwargs([names.index(c) for c in columns if c in names])


def _scan_record_widths(
    open_handle: OpenHandle, dialect: CsvDialect, *, table: str, nrows: int | None
) -> None:
    """Refuse when a consumed record's field count differs from the declared names.

    Scans only the records the read consumes (`nrows`), so a bounded sample does not walk a
    huge file. Reads through the policy-issued handle.
    """
    expected = len(dialect.column_names or ())
    consumed = 0
    with open_handle() as handle, handle.open_binary() as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = _record_reader(
            text,
            dialect.delimiter,
            on_nul=lambda: source_refusal(
                CODES.SOURCE_FILE_NUL_BYTE,
                table,
                "contains a NUL byte, which is not valid in a text delimited file",
            ),
            on_lone_cr=lambda: source_refusal(
                CODES.SOURCE_FILE_LONE_CR,
                table,
                "contains a lone carriage return (only LF and CRLF line endings are supported)",
            ),
        )
        for record in reader:
            if _is_blank_record(record):
                continue
            if len(record) != expected:
                raise source_refusal(
                    CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
                    table,
                    f"line {reader.line_num} has {len(record)} fields "
                    f"but column_names declares {expected}",
                )
            consumed += 1
            if nrows is not None and consumed >= nrows:
                return
    if consumed == 0:
        raise source_refusal(
            CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
            table,
            f"has no data rows to read against its {expected} declared column_names",
        )


def _zero_rows(
    open_handle: OpenHandle, names: list[str], *, chunked: bool
) -> pd.DataFrame | Iterator[pd.DataFrame]:
    """The headerless `nrows=0` result, without tokenizing a byte of content.

    Opening the handle runs the file-access policy and `stat` proves the file exists, so a
    missing or denied path fails here as it does for any read. The empty frame matches what
    pandas returns for `nrows=0` (object columns, empty RangeIndex); a chunked read yields
    no chunk, as pandas does.
    """
    with open_handle() as handle:
        handle.stat()
    if chunked:
        return iter(())
    return pd.DataFrame({name: pd.Series(dtype=object) for name in names})


def _unreadable(table: str, expected: int) -> PipelineValidationError:
    return source_refusal(
        CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
        table,
        f"could not be read against its {expected} declared column_names",
    )


def _named(frame: pd.DataFrame, names: list[str], table: str) -> pd.DataFrame:
    """Assign the declared names; backstop for a parser/scan disagreement on field count.

    `names` are the names of the columns the read kept, in file order."""
    if frame.shape[1] != len(names):
        raise source_refusal(
            CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
            table,
            f"parsed to {frame.shape[1]} columns against its {len(names)} declared column_names",
        )
    frame.columns = names
    return frame


def _ordered(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """`frame` holding exactly `columns`, in that order. pandas ignores the order of `usecols`,
    so the requested order is restored here, once, for every frame and chunk."""
    if len(frame.columns) != len(columns) or set(frame.columns) != set(columns):
        raise ValueError("read_csv_source: columns names a column the file does not have")
    return frame[list(columns)]


def _shaped(result: Any, columns: Sequence[str]) -> Any:
    """`_ordered` over one frame, or lazily over each chunk of an iterator."""
    if isinstance(result, pd.DataFrame):
        return _ordered(result, columns)
    return (_ordered(chunk, columns) for chunk in result)


def _projection(
    dialect: CsvDialect, columns: Sequence[str]
) -> tuple[dict[str, Any], list[str] | None]:
    """The pandas `usecols` for `columns`, and the names the kept columns carry in file order
    (headerless only; with a header pandas names them itself).

    With a header a callable keeps a missing name from raising a pandas error that quotes it;
    `_ordered` then refuses the mismatch with a fixed message."""
    if not columns:
        raise ValueError("read_csv_source: columns names no column")
    if len(set(columns)) != len(columns):
        raise ValueError("read_csv_source: columns names a column more than once")
    if dialect.has_header:
        wanted = frozenset(columns)
        return {"usecols": lambda name: name in wanted}, None
    declared = list(dialect.column_names or ())
    if any(c not in declared for c in columns):
        raise ValueError("read_csv_source: columns names a column the file does not have")
    positions = sorted(declared.index(c) for c in columns)
    return {"usecols": positions}, [declared[p] for p in positions]


def _read_whole(
    open_handle: OpenHandle,
    kwargs: dict[str, Any],
    names: list[str] | None,
    table: str,
    kept: list[str] | None = None,
) -> pd.DataFrame:
    # Headerless, the adapter lets a pandas parser error through a strict policy (its text can
    # carry a cell) so it is replaced here by the coded refusal, which chains nothing. With a
    # header there is no coded refusal: the plain adapter maps it to a read error, and the
    # ambient policy keeps the library error as before.
    read = pandas_adapter.read_csv if names is None else pandas_adapter.read_csv_parser_errors
    failed = False
    try:
        with open_handle() as handle:
            frame = read(handle, **kwargs)
    except _PARSER_ERRORS:
        if names is None:
            raise
        failed = True  # raised below, outside the except block, so nothing is chained
    if failed:
        raise _unreadable(table, len(names or ()))
    return frame if names is None else _named(frame, kept or names, table)


def _read_chunks(
    open_handle: OpenHandle,
    kwargs: dict[str, Any],
    names: list[str] | None,
    table: str,
    kept: list[str] | None = None,
) -> Iterator[pd.DataFrame]:
    read = (
        pandas_adapter.read_csv_chunks
        if names is None
        else pandas_adapter.read_csv_chunks_parser_errors
    )
    failed = False
    try:
        with open_handle() as handle:
            for chunk in read(handle, **kwargs):
                yield chunk if names is None else _named(chunk, kept or names, table)
    except _PARSER_ERRORS:
        if names is None:
            raise
        failed = True
    if failed:
        raise _unreadable(table, len(names or ()))


def _read_csv_rows(
    open_handle: OpenHandle,
    dialect: CsvDialect,
    *,
    table: str,
    nrows: int | None = None,
    chunksize: int | None = None,
    dtype: type[str] | None = None,
    lexeme_columns: tuple[str, ...] = (),
    columns: Sequence[str] | None = None,
    exact_text: bool = False,
) -> pd.DataFrame | Iterator[pd.DataFrame]:
    """The shared core: one frame, or an iterator of frames when `chunksize` is set.

    `exact_text` turns off pandas' NA vocabulary for the whole frame, so `NA` and an empty field
    reach the caller as the text they are (`read_csv_bytes` needs that: `NA` is a country code).

    With `columns`, only those columns are parsed and every frame holds exactly them, in the
    order given.

    Converter keys are built here, once: by name with a header, by position headerless
    (pandas ignores a name key when `header=None`). A headerless read scans the whole file
    (or `nrows` records) at the call; `nrows=0` skips the scan and the parse, before returning either result. The per-chunk width
    check is only a backstop for a parser disagreement; the scan is the guard.
    """
    names: list[str] | None = None
    kept: list[str] | None = None
    read_opts: dict[str, Any]
    use: dict[str, Any] = {}
    if columns is not None:
        use, kept = _projection(dialect, columns)
    if dialect.has_header:
        read_opts = pandas_lexeme_kwargs(lexeme_columns)
    else:
        names = list(dialect.column_names or ())
        if nrows == 0:
            # No record is consumed, so none is scanned or tokenized: a malformed file reads
            # as the empty frame under every policy.
            return _zero_rows(
                open_handle,
                names if columns is None else list(columns),
                chunked=chunksize is not None,
            )
        _scan_record_widths(open_handle, dialect, table=table, nrows=nrows)
        read_opts = {"header": None, **_positional_lexemes(lexeme_columns, names)}
    kwargs: dict[str, Any] = {"sep": dialect.delimiter, "dtype": dtype, **read_opts, **use}
    if exact_text:
        kwargs = {**kwargs, "keep_default_na": False, "na_filter": False}
    if nrows is not None:
        kwargs["nrows"] = nrows
    if chunksize is None:
        frame = _read_whole(open_handle, kwargs, names, table, kept)
        return frame if columns is None else _shaped(frame, columns)
    if dialect.has_header:
        with open_handle() as handle:  # a missing or denied file fails at the call, as headerless
            handle.stat()
    chunks = _read_chunks(open_handle, {**kwargs, "chunksize": chunksize}, names, table, kept)
    return chunks if columns is None else _shaped(chunks, columns)


@overload
def read_csv_source(
    path: str | os.PathLike[str],
    dialect: CsvDialect,
    *,
    table: str,
    dtype: type[str] | None = ...,
    nrows: int | None = ...,
    chunksize: None = ...,
    file_access: FileAccess | None = ...,
    field_key: str = ...,
    columns: Sequence[str] | None = ...,
) -> pd.DataFrame: ...


@overload
def read_csv_source(
    path: str | os.PathLike[str],
    dialect: CsvDialect,
    *,
    table: str,
    dtype: type[str] | None = ...,
    nrows: int | None = ...,
    chunksize: int,
    file_access: FileAccess | None = ...,
    field_key: str = ...,
    columns: Sequence[str] | None = ...,
) -> Iterator[pd.DataFrame]: ...


@fileaccess.entry_point
def read_csv_source(
    path: str | os.PathLike[str],
    dialect: CsvDialect,
    *,
    table: str,
    dtype: type[str] | None = None,
    nrows: int | None = None,
    chunksize: int | None = None,
    file_access: FileAccess | None = None,
    field_key: str = "source.path",
    columns: Sequence[str] | None = None,
) -> pd.DataFrame | Iterator[pd.DataFrame]:
    """Read the CSV file at `path` under `dialect`.

    `dtype=None` lets pandas infer (what profiling uses); `dtype=str` reads every cell as
    text (the CLI run contract). With the default dialect and `dtype=str` the frame equals
    `pd.read_csv(path, dtype=str)`. A headerless file is width-checked first: a wrong field
    count, no data rows, a NUL byte, a lone CR or a parser disagreement raises
    `PipelineValidationError` with a registered code and a value-free message that starts
    with the code and names `table`.

    With `chunksize` the whole-file scan runs at the call (it raises there) and the result
    is an iterator of frames of at most `chunksize` rows, bound to the call's file-access
    policy. A parser disagreement the scan cannot see (an unterminated quote) surfaces on
    the step that reads it.

    `columns` parses only the named columns and returns every frame or chunk with exactly
    them, in the order given (not file order). A name the file lacks, or a repeated name, is a
    `ValueError` that quotes no name; a headerless file maps the names onto `column_names`.
    """
    if dtype is not None and dtype is not str:
        raise TypeError("read_csv_source: dtype must be None or str")
    if chunksize is not None and (not isinstance(chunksize, int) or chunksize < 1):
        raise ValueError("read_csv_source: chunksize must be a positive integer")
    access = fileaccess.current()
    request = FileRequest(FileRole.SOURCE, "read", str(path), field_key)

    def open_handle() -> FileHandle:
        return access.open_read(request)

    return _read_csv_rows(
        open_handle,
        dialect,
        table=table,
        nrows=nrows,
        chunksize=chunksize,
        dtype=dtype,
        columns=columns,
    )


def read_csv_bytes(raw: bytes, *, table: str, field_key: str = "lookup.file") -> pd.DataFrame:
    """Parse CSV bytes the engine already holds, as exact text.

    The one bytes seam of the CSV reader (a pinned lookup file is read once through the file
    policy, then parsed from the plan's bytes with no second open). One fixed dialect: comma
    delimiter, a header row, UTF-8 (a BOM is stripped), every cell kept as text with no NA
    conversion. It goes through `_read_csv_rows` over a `MemoryHandle`, so it shares the reader's
    parse and its strict failure handling: a malformed file raises `FileAccessReadError` naming
    only `field_key`, never a cell. `table` names the source in any `PipelineValidationError`.
    """
    handle = MemoryHandle(raw, field_key)
    frame = _read_csv_rows(lambda: handle, CsvDialect(), table=table, dtype=str, exact_text=True)
    assert isinstance(frame, pd.DataFrame)  # noqa: S101 - chunksize is None
    return frame


__all__ = ["CsvDialect", "read_csv_bytes", "read_csv_source", "resolve_csv_dialect"]

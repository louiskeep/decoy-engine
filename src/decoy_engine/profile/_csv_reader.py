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

Every refusal is a `PipelineValidationError` with a registered code, built by `_refusal`:
`<code>: source <table> <detail>`. The detail names the line and the counts, never a cell
value, a path or a file name, and the library error is never chained.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, overload

import pandas as pd

from decoy_engine import fileaccess
from decoy_engine._lexeme import pandas_lexeme_kwargs
from decoy_engine.config._csv_dialect import CsvDialect
from decoy_engine.errors import PipelineValidationError
from decoy_engine.fileaccess import FileAccess, FileHandle, FileRequest, FileRole
from decoy_engine.fileaccess.adapters import pandas as pandas_adapter
from decoy_engine.validation_result import CODES

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


def resolve_csv_dialect(descriptor: Mapping[str, Any]) -> CsvDialect:
    """The `CsvDialect` a source descriptor declares; omitted keys take the schema defaults.

    The only place in the engine that reads the three dialect keys of a descriptor.
    """
    # Only the keys present are passed, so `CsvDialect`'s field defaults are the one home of
    # the defaults and `CsvDialect` alone validates and normalizes the values.
    present = {
        f.name: descriptor[f.name] for f in dataclasses.fields(CsvDialect) if f.name in descriptor
    }
    return CsvDialect(**present)


def _refusal(code: str, table: str, detail: str) -> PipelineValidationError:
    """The one constructor of this module's refusals: coded, names the table, no value."""
    return PipelineValidationError(f"{code}: source {table} {detail}", code=code)


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
    if nrows == 0:
        return  # no record is consumed, so none is checked; the read returns the empty frame
    expected = len(dialect.column_names or ())
    consumed = 0
    with open_handle() as handle, handle.open_binary() as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")

        def _reject_unreconcilable(lines: Iterable[str]) -> Iterator[str]:
            # With newline="" the wrapper splits on CR, LF and CRLF, so a lone-CR ending
            # leaves a trailing "\r" and CRLF a trailing "\r\n". Strip one LF or CRLF; any
            # "\r" left is a lone CR.
            for line in lines:
                if "\x00" in line:
                    raise _refusal(
                        CODES.SOURCE_FILE_NUL_BYTE,
                        table,
                        "contains a NUL byte, which is not valid in a text delimited file",
                    )
                body = line[:-2] if line.endswith("\r\n") else line.rstrip("\n")
                if "\r" in body:
                    raise _refusal(
                        CODES.SOURCE_FILE_LONE_CR,
                        table,
                        "contains a lone carriage return "
                        "(only LF and CRLF line endings are supported)",
                    )
                yield line

        reader = csv.reader(_reject_unreconcilable(text), delimiter=dialect.delimiter)
        for record in reader:
            if len(record) <= 1 and (not record or not record[0].strip()):
                continue
            if len(record) != expected:
                raise _refusal(
                    CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
                    table,
                    f"line {reader.line_num} has {len(record)} fields "
                    f"but column_names declares {expected}",
                )
            consumed += 1
            if nrows is not None and consumed >= nrows:
                return
    if consumed == 0:
        raise _refusal(
            CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
            table,
            f"has no data rows to read against its {expected} declared column_names",
        )


def _unreadable(table: str, expected: int) -> PipelineValidationError:
    return _refusal(
        CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
        table,
        f"could not be read against its {expected} declared column_names",
    )


def _named(frame: pd.DataFrame, names: list[str], table: str) -> pd.DataFrame:
    """Assign the declared names; backstop for a parser/scan disagreement on field count."""
    if frame.shape[1] != len(names):
        raise _refusal(
            CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
            table,
            f"parsed to {frame.shape[1]} columns against its {len(names)} declared column_names",
        )
    frame.columns = names
    return frame


def _read_whole(
    open_handle: OpenHandle,
    kwargs: dict[str, Any],
    names: list[str] | None,
    table: str,
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
    return frame if names is None else _named(frame, names, table)


def _read_chunks(
    open_handle: OpenHandle,
    kwargs: dict[str, Any],
    names: list[str] | None,
    table: str,
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
                yield chunk if names is None else _named(chunk, names, table)
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
) -> pd.DataFrame | Iterator[pd.DataFrame]:
    """The shared core: one frame, or an iterator of frames when `chunksize` is set.

    Converter keys are built here, once: by name with a header, by position headerless
    (pandas ignores a name key when `header=None`). A headerless read scans the whole file
    (or `nrows` records) at the call, before returning either result. The per-chunk width
    check is only a backstop for a parser disagreement; the scan is the guard.
    """
    names: list[str] | None = None
    read_opts: dict[str, Any]
    if dialect.has_header:
        read_opts = pandas_lexeme_kwargs(lexeme_columns)
    else:
        names = list(dialect.column_names or ())
        _scan_record_widths(open_handle, dialect, table=table, nrows=nrows)
        read_opts = {"header": None, **_positional_lexemes(lexeme_columns, names)}
        if nrows == 0:
            # Zero rows: name the empty frame at the read, so a malformed first record (which
            # was not scanned) cannot change its width.
            read_opts["names"] = names
    kwargs: dict[str, Any] = {"sep": dialect.delimiter, "dtype": dtype, **read_opts}
    if nrows is not None:
        kwargs["nrows"] = nrows
    if chunksize is None:
        return _read_whole(open_handle, kwargs, names, table)
    if dialect.has_header:
        with open_handle() as handle:  # a missing or denied file fails at the call, as headerless
            handle.stat()
    return _read_chunks(open_handle, {**kwargs, "chunksize": chunksize}, names, table)


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
        open_handle, dialect, table=table, nrows=nrows, chunksize=chunksize, dtype=dtype
    )


__all__ = ["CsvDialect", "read_csv_source", "resolve_csv_dialect"]

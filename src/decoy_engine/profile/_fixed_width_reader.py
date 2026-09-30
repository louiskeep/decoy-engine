"""Fixed-width file reader: turns a newline-delimited fixed-width file
into a pandas DataFrame per a `FixedWidthLayout` column-spec (S4,
engine-finish-open-ended program).

Record model (frozen by `FixedWidthLayout`, see `config._fixed_width`):
each line in the file is one record. Every column is sliced from that
line by its `[start, start + width)` half-open byte range (0-based
`start`), the declared pad character is stripped from the side implied
by `align`, and the stripped string is cast to the column's declared
type.

Deliberately hand-rolled rather than `pandas.read_fwf`: pandas' fixed-
width reader assumes whitespace-only field padding and does its own
implicit dtype/NaN inference, neither of which honors an arbitrary
`pad` character or this module's fail-closed cast contract (no silent
coercion to NaN/default on a bad value). Direct `(start, width)`
slicing is the plain, established convention for fixed-width records
(the same one `pandas.read_fwf`'s `colspecs` uses internally) with full
control over padding and casting.

Fail-closed, no silent truncation or coercion: a line shorter than the
layout's required width raises before any column is sliced from it; a
value that fails its declared type-cast raises naming the column, never
substituting a default. Neither error embeds the offending cell value,
and neither chains it in via `__cause__` or `__context__` (see
`errors.FixedWidthParseError` and `_cast_value` below) -- source files
may carry PII, and a chained cause OR context leaks through
tracebacks/`logging.exception`/`exc_info=True` even when the message
itself is clean. Bad data never raises inside the code that holds it:
`_cast_value` and `_parse_records` return a failure message (position,
column name, value length, caster type name) instead of raising, and
`read_fixed_width` raises only after they have returned, from a frame
that holds no line, value or record. This covers errors about the data;
a failure that is not (a disk error mid-read, running out of memory) may
surface from a frame holding records, as in any data-processing code --
see docs/security/error-reporting-and-data-exposure.md. An exception raised while another is
being handled gets it attached as `__context__` even with `from None`,
which is why no raise happens inside an `except` block here.

Zero-padded numerics: an `int`/`float` column whose pad character
strips the value down to `""` is retried against the RAW (unstripped)
slice before erroring, so a genuine zero-padded numeric (pad="0",
align="right", raw "0000") parses to `0` rather than failing. An
honestly-blank numeric field (raw is pure whitespace/pad with no
digits, e.g. "   ") still fails the cast and raises -- whitespace is
never silently coerced to a default.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
from pydantic import ValidationError as PydanticValidationError

from decoy_engine.config._fixed_width import FixedWidthColumn, FixedWidthLayout
from decoy_engine.errors import ConfigError, FixedWidthParseError

_CASTERS: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
}


def _strip_pad(raw: str, column: FixedWidthColumn) -> str:
    """Strip `column.pad` from the side implied by `column.align`."""
    if column.align == "left":
        return raw.rstrip(column.pad)
    return raw.lstrip(column.pad)


def _cast_value(
    stripped: str, raw: str, column: FixedWidthColumn, *, path: str, line_no: int
) -> tuple[Any, str | None]:
    """Return `(value, None)`, or `(None, message)` when the cast fails.

    Never raises: the caller raises from a frame that holds no file data (see
    `read_fixed_width`), and the message carries only position, column name
    and value length, never the value.
    """
    caster = _CASTERS[column.type]
    candidate = stripped
    if stripped == "" and column.type in ("int", "float"):
        # A legitimate zero-padded numeric (e.g. pad="0", align="right",
        # raw "0000") strips down to "" -- `_strip_pad` can't tell "all
        # digits happen to equal the pad character" apart from "there is
        # no value here". Retry the cast on the RAW (unstripped) slice:
        # `int("0000") == 0` succeeds for genuine zero-padded data, while
        # an honestly-blank numeric field (raw is pure pad/whitespace,
        # e.g. "   ") still fails `int("   ")`/`float("   ")` -- whitespace
        # is never silently coerced to 0.
        candidate = raw
    try:
        return caster(candidate), None
    except (ValueError, TypeError) as exc:
        # The caught exception's text embeds the raw value, so only its type
        # name is disclosed.
        return None, (
            f"{_render_path(path)}: line {line_no}: column {column.name!r} "
            f"(value length {len(candidate)}) cannot cast to type {column.type!r} "
            f"(caster raised {type(exc).__name__})"
        )


def _render_path(path: str) -> str:
    """`path` with control and unpaired-surrogate characters escaped, so an
    error message stays one line and always encodes as UTF-8."""
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in path)


def _resolve_layout(layout: FixedWidthLayout | dict[str, Any], *, path: str) -> FixedWidthLayout:
    """Return `layout` as a `FixedWidthLayout`, validating a dict.

    A dict is schema-shape detail (field names, declared widths/types),
    not file content, but there is no reason to chain the pydantic error
    in either: only the error count is kept, and the wrapper is raised
    after the `except` block has exited (see the module docstring).
    """
    if isinstance(layout, FixedWidthLayout):
        return layout
    error_count = 0
    try:
        return FixedWidthLayout.model_validate(layout)
    except PydanticValidationError as exc:
        error_count = exc.error_count()
    raise ConfigError(
        f"{_render_path(path)}: fixed_width layout failed schema validation "
        f"({error_count} error(s)); see FixedWidthLayout for the expected shape."
    )


def read_fixed_width(
    path: str | os.PathLike[str],
    layout: FixedWidthLayout | dict[str, Any],
    *,
    max_records: int | None = None,
) -> pd.DataFrame:
    """Parse a fixed-width file at `path` into a DataFrame per `layout`.

    Args:
        path: filesystem path (`str` or `os.PathLike[str]`) to the
            newline-delimited fixed-width file. Records end at `\n`; a
            trailing `\r` is stripped, so CRLF files read the same.
        layout: a `FixedWidthLayout` instance, or the equivalent plain
            dict a validated `PipelineConfig` carries at `FileSource.layout`
            (e.g. `PipelineConfig.model_validate(...).model_dump()`'s
            output for that field). A dict is re-validated through
            `FixedWidthLayout.model_validate`, so a caller handing in a
            malformed dict fails loud here too, as a `ConfigError`,
            rather than propagating a raw pydantic `ValidationError` or a
            `KeyError`/`AttributeError`.
        max_records: SC7a bounded-read cap. `None` (default) reads every
            record. Otherwise must be a non-negative `int`: the file is
            read only up to and including the line that produces the
            `max_records`-th record: lines are read in binary one at a
            time, so no bytes past that line are decoded or examined
            (buffered I/O may still fetch them from disk) -- a bounded
            profiling sample never reads one line past what it needed,
            let alone the whole file. `0` reads zero lines. A skipped
            blank line does not count against the cap.

    Returns:
        One row per non-blank line (or `max_records` of them, if capped),
        one column per `layout.columns` entry (in layout order), each
        cast to its declared type. A wholly blank line (zero characters
        once the newline is stripped) is skipped -- it carries no data to
        lose, matching the blank-line convention of `pandas.read_csv`.

    Raises:
        TypeError: `max_records` is not an `int` (a `bool` counts as not
            an `int` here, since `True`/`False` as a record count is
            almost certainly a caller mistake, not an intentional 0/1), or
            `path` is not a `str` or does not resolve to one via
            `os.fspath` (e.g. a `bytes` path).
        ValueError: `max_records` is negative, or `path` contains a NUL
            character.
        ConfigError: `layout` is a dict that fails `FixedWidthLayout`'s
            schema validation.
        FixedWidthParseError: a non-blank line is shorter than the
            layout's required width (`FixedWidthLayout.record_width`), a
            sliced value fails its column's declared cast, or a line's
            bytes are not valid UTF-8 (reported with that line's number).
        OSError: `path` does not exist or cannot be opened (e.g. the
            built-in `FileNotFoundError`, `PermissionError`, `IsADirectoryError`).
            Passed through unwrapped -- these are ordinary filesystem
            errors, not fixed-width-specific ones, and carry no file
            content to leak.
    """
    if isinstance(max_records, bool) or not isinstance(max_records, (int, type(None))):
        raise TypeError(f"max_records must be an int or None, got {type(max_records).__name__}")
    if max_records is not None and max_records < 0:
        raise ValueError(f"max_records must be >= 0, got {max_records}")

    path = os.fspath(path)
    if not isinstance(path, str):
        raise TypeError(f"path must be a str or os.PathLike[str], got {type(path).__name__}")
    if "\x00" in path:
        raise ValueError("path must not contain a NUL character")
    spec = _resolve_layout(layout, path=path)

    records, failure = _parse_records(path, spec, max_records)
    if failure is not None:
        # Raised here, after `_parse_records` has returned: this frame holds
        # no line, value or parsed record, so nothing reachable from the
        # exception (chain, traceback frames, captured locals) carries file
        # content.
        raise FixedWidthParseError(failure)
    if records is None:  # pragma: no cover - _parse_records returns one or the other
        raise RuntimeError("fixed-width parser returned neither records nor a failure")

    column_names = [column.name for column in spec.columns]
    return pd.DataFrame.from_records(records, columns=column_names)


__all__ = ["read_fixed_width"]


def _parse_records(
    path: str, spec: FixedWidthLayout, max_records: int | None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Parse up to `max_records` records. Returns `(records, None)`, or
    `(None, message)` on the first bad line; never raises for bad data, so
    no exception is ever created in a frame that holds file content."""
    required_width = spec.record_width
    records: list[dict[str, Any]] = []
    line_no = 0
    rendered = _render_path(path)
    # Binary, one physical line per read: a text-mode reader decodes ahead in
    # ~8 KB chunks, so a capped read could fail on bytes past the cap and a
    # decode error could not name its real line.
    with open(path, "rb") as fh:
        while max_records is None or len(records) < max_records:
            raw_bytes = fh.readline()
            if raw_bytes == b"":
                break  # EOF
            line_no += 1
            try:
                raw_line = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return None, f"{rendered}: line {line_no}: not valid UTF-8 text"
            line = raw_line.rstrip("\r\n")
            if line == "":
                continue
            if len(line) < required_width:
                return None, (
                    f"{rendered}: line {line_no}: record is {len(line)} chars, "
                    f"shorter than the layout's required {required_width} chars "
                    "(row-width mismatch)"
                )
            row: dict[str, Any] = {}
            for column in spec.columns:
                raw_value = line[column.start : column.start + column.width]
                stripped = _strip_pad(raw_value, column)
                value, cast_failure = _cast_value(
                    stripped, raw_value, column, path=path, line_no=line_no
                )
                if cast_failure is not None:
                    return None, cast_failure
                row[column.name] = value
            records.append(row)
    return records, None

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
itself is clean. `_cast_value` gets this right by capturing only the
caster's type name inside its `except` block, then raising
`FixedWidthParseError` after that block has exited: Python auto-attaches
`__context__` to a newly raised exception only while another exception
is actively being handled, so `raise` from OUTSIDE the handler carries
no context no matter what. Raising `from None` inside the handler is
NOT equivalent -- `__context__` is (re)populated by the `raise`
statement itself at the moment it executes, so it silently overwrites
any `some_exc.__context__ = None` set beforehand in the same handler.

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
) -> Any:
    caster = _CASTERS[column.type]
    candidate = stripped
    if stripped == "" and column.type in ("int", "float"):
        # A legitimate zero-padded numeric (e.g. pad="0", align="right",
        # raw "0000") strips down to "" -- `_strip_pad` can't tell "all
        # digits happen to equal the pad character" apart from "there is
        # no value here". Retry the cast on the RAW (unstripped) slice:
        # `int("0000") == 0` succeeds for genuine zero-padded data, while
        # an honestly-blank numeric field (raw is pure pad/whitespace,
        # e.g. "   ") still fails `int("   ")`/`float("   ")` and raises
        # below -- whitespace is never silently coerced to 0.
        candidate = raw
    cast_failure: str | None = None
    try:
        return caster(candidate)
    except (ValueError, TypeError) as exc:
        # The caught ValueError/TypeError's own text embeds the raw
        # offending value (e.g. "invalid literal for int() with base 10:
        # 'SECRET-1234'"), so only its type name is safe to disclose.
        # Build the message here, but do NOT raise here: Python
        # auto-attaches this exception as `__context__` to anything
        # raised while this handler is active, REGARDLESS of `from None`
        # or an explicit `some_exc.__context__ = None` -- the `raise`
        # statement (re)populates `__context__` at the moment it runs,
        # silently overwriting a prior assignment. Recording the message
        # and raising once this `except` block has exited (below) means
        # there is no exception being handled at raise time, so Python
        # has nothing to attach.
        cast_failure = (
            f"{path}: line {line_no}: column {column.name!r} "
            f"(value length {len(candidate)}) cannot cast to type {column.type!r} "
            f"(caster raised {type(exc).__name__})"
        )
    # Outside the `except` block: no exception is being handled here, so
    # this `raise` gets `__cause__ is None` and `__context__ is None` for
    # free, with no `from None` needed.
    raise FixedWidthParseError(cast_failure)


def _resolve_layout(layout: FixedWidthLayout | dict[str, Any], *, path: str) -> FixedWidthLayout:
    """Return `layout` as a `FixedWidthLayout`, validating a dict.

    A dict is schema-shape detail (field names, declared widths/types),
    not file content, but there is no reason to chain the pydantic error
    in either -- raise the safe wrapper once the `except` block below has
    exited (see `_cast_value`'s comment for why raising INSIDE the
    handler cannot avoid `__context__`).
    """
    if isinstance(layout, FixedWidthLayout):
        return layout
    layout_error: PydanticValidationError | None = None
    try:
        return FixedWidthLayout.model_validate(layout)
    except PydanticValidationError as exc:
        layout_error = exc
    raise ConfigError(
        f"{path}: fixed_width layout failed schema validation "
        f"({layout_error.error_count()} error(s)); see FixedWidthLayout "
        "for the expected shape."
    )


def read_fixed_width(
    path: str | os.PathLike[str],
    layout: FixedWidthLayout | dict[str, Any],
    *,
    max_records: int | None = None,
) -> pd.DataFrame:
    """Parse a fixed-width file at `path` into a DataFrame per `layout`.

    Args:
        path: filesystem path to the newline-delimited fixed-width file.
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
            `max_records`-th record, and no further -- a bounded
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
            almost certainly a caller mistake, not an intentional 0/1).
        ValueError: `max_records` is negative.
        ConfigError: `layout` is a dict that fails `FixedWidthLayout`'s
            schema validation.
        FixedWidthParseError: a non-blank line is shorter than the
            layout's required width (`FixedWidthLayout.record_width`), a
            sliced value fails its column's declared cast, or the file's
            bytes are not valid UTF-8 text.
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
    spec = _resolve_layout(layout, path=path)
    required_width = spec.record_width

    records: list[dict[str, Any]] = []
    line_no = 0
    decode_error_line: int | None = None
    with open(path, encoding="utf-8") as fh:
        while max_records is None or len(records) < max_records:
            try:
                raw_line = fh.readline()
            except UnicodeDecodeError:
                # `exc.object` holds the raw undecodable bytes, which may
                # themselves be (partially) the PII this reader exists to
                # mask -- do not touch it. Record only the position and
                # break; the safe wrapper raises below, outside this
                # handler, once the `with` block (and this handler) has
                # closed.
                decode_error_line = line_no + 1
                break
            if raw_line == "":
                break  # EOF
            line_no += 1
            line = raw_line.rstrip("\r\n")
            if line == "":
                continue
            if len(line) < required_width:
                raise FixedWidthParseError(
                    f"{path}: line {line_no}: record is {len(line)} chars, "
                    f"shorter than the layout's required {required_width} chars "
                    "(row-width mismatch)"
                )
            row: dict[str, Any] = {}
            for column in spec.columns:
                raw_value = line[column.start : column.start + column.width]
                stripped = _strip_pad(raw_value, column)
                row[column.name] = _cast_value(
                    stripped, raw_value, column, path=path, line_no=line_no
                )
            records.append(row)

    if decode_error_line is not None:
        raise FixedWidthParseError(
            f"{path}: line {decode_error_line}: file is not valid UTF-8 text"
        )

    column_names = [column.name for column in spec.columns]
    return pd.DataFrame.from_records(records, columns=column_names)


__all__ = ["read_fixed_width"]

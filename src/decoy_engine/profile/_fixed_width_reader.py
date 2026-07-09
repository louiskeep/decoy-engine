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
substituting a default. Neither error embeds the offending cell value
or chains it in as the raised exception's `__cause__` (see
`errors.FixedWidthParseError` and `_cast_value`'s `from None`) --
source files may carry PII, and a chained cause leaks through
tracebacks/`logging.exception`/`exc_info=True` even when the message
itself is clean.

Zero-padded numerics: an `int`/`float` column whose pad character
strips the value down to `""` is retried against the RAW (unstripped)
slice before erroring, so a genuine zero-padded numeric (pad="0",
align="right", raw "0000") parses to `0` rather than failing. An
honestly-blank numeric field (raw is pure whitespace/pad with no
digits, e.g. "   ") still fails the cast and raises -- whitespace is
never silently coerced to a default.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.config._fixed_width import FixedWidthColumn, FixedWidthLayout
from decoy_engine.errors import FixedWidthParseError

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
    try:
        return caster(candidate)
    except (ValueError, TypeError) as exc:
        # `from None` deliberately severs the exception chain: the caught
        # ValueError/TypeError's own text embeds the raw offending value
        # (e.g. "invalid literal for int() with base 10: 'SECRET-1234'"),
        # and a chained cause propagates via `__cause__` into tracebacks,
        # `logging.exception`, and `exc_info=True` output even though this
        # message never repeats it. A bare `raise` (no `from`) is NOT safe
        # here either -- it still attaches the original as `__context__`,
        # which the same log/traceback surfaces leak through just as
        # readily. Only the caster's type name is safe to disclose.
        exc_to_raise = FixedWidthParseError(
            f"{path}: line {line_no}: column {column.name!r} "
            f"(value length {len(candidate)}) cannot cast to type {column.type!r} "
            f"(caster raised {type(exc).__name__})"
        )
        exc_to_raise.__context__ = None
        raise exc_to_raise from None


def read_fixed_width(
    path: str,
    layout: FixedWidthLayout | dict[str, Any],
    *,
    max_records: int | None = None,
) -> pd.DataFrame:
    """Parse a fixed-width file at `path` into a DataFrame per `layout`.

    Args:
        path: filesystem path to the newline-delimited fixed-width file.
        layout: a `FixedWidthLayout` instance, or a plain dict shaped
            like one (e.g. the nested dict `PipelineConfig.model_validate
            (...).model_dump()` produces for `FileSource.layout`). Dicts
            are re-validated through `FixedWidthLayout.model_validate`,
            so a caller handing in a malformed dict fails loud here too
            rather than propagating a raw `KeyError`/`AttributeError`.
        max_records: SC7a bounded-read cap. When set, parsing stops after
            this many data records so a bounded profiling sample never reads
            the whole file. None (default) reads every record, unchanged.

    Returns:
        One row per non-blank line, one column per `layout.columns`
        entry (in layout order), each cast to its declared type. A
        wholly blank line (zero characters once the newline is
        stripped) is skipped -- it carries no data to lose, matching
        the blank-line convention of `pandas.read_csv`.

    Raises:
        FixedWidthParseError: a non-blank line is shorter than the
            layout's required width (`FixedWidthLayout.record_width`),
            or a sliced value fails its column's declared cast.
    """
    spec = (
        layout if isinstance(layout, FixedWidthLayout) else FixedWidthLayout.model_validate(layout)
    )
    required_width = spec.record_width

    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            if max_records is not None and len(records) >= max_records:
                break
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

    column_names = [column.name for column in spec.columns]
    return pd.DataFrame.from_records(records, columns=column_names)


__all__ = ["read_fixed_width"]

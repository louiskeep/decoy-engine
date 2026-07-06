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
(see `errors.FixedWidthParseError`) -- source files may carry PII.
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


def _cast_value(stripped: str, column: FixedWidthColumn, *, path: str, line_no: int) -> Any:
    caster = _CASTERS[column.type]
    try:
        return caster(stripped)
    except (ValueError, TypeError) as exc:
        raise FixedWidthParseError(
            f"{path}: line {line_no}: column {column.name!r} "
            f"(value length {len(stripped)}) cannot cast to type {column.type!r}"
        ) from exc


def read_fixed_width(path: str, layout: FixedWidthLayout | dict[str, Any]) -> pd.DataFrame:
    """Parse a fixed-width file at `path` into a DataFrame per `layout`.

    Args:
        path: filesystem path to the newline-delimited fixed-width file.
        layout: a `FixedWidthLayout` instance, or a plain dict shaped
            like one (e.g. the nested dict `PipelineConfig.model_validate
            (...).model_dump()` produces for `FileSource.layout`). Dicts
            are re-validated through `FixedWidthLayout.model_validate`,
            so a caller handing in a malformed dict fails loud here too
            rather than propagating a raw `KeyError`/`AttributeError`.

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
                row[column.name] = _cast_value(stripped, column, path=path, line_no=line_no)
            records.append(row)

    column_names = [column.name for column in spec.columns]
    return pd.DataFrame.from_records(records, columns=column_names)


__all__ = ["read_fixed_width"]

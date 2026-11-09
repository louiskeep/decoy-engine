"""Cell format of a fitted column: the shape of its cells, learned at fit and checked on read.

A snapshot column entry can carry a `cell_format` block that records how the source cells
were written, so generation can reproduce it: the decimal places of a numeric column, the
calendar resolution of a datetime column and the blank count of a freetext column. This module
holds the fit-side half (`learn_cell_format`, `add_cell_format`) and the one descriptor of which
members each kind may carry (`CELL_FORMAT_MEMBERS`). The snapshot redactor reuses that
descriptor, so a block can never carry a member the reader would not accept.

Every member is an `int` count, an `int` place count or a member of a fixed enum, never a source
cell, so the redacted fit writes the block for sensitive columns too.

Pattern: SDV RDT `FloatFormatter(learn_rounding_scheme=True)` (`learn_rounding_digits`) for
decimal places, and `OptimizedTimestampEncoder._find_divider` for resolution; both published
methods, no licence. See: https://docs.sdv.dev/rdt/transformers-glossary/numerical/floatformatter

Method notes:
  - Decimal places is the smallest `d` from 0 up at which every finite cell equals itself rounded
    to `d` places, judged in the float width the source stored (a float32 column is rounded as
    float32). RDT caps the search at the width's decimal precision (`sys.float_info.dig` for
    float64) minus the digits of the largest magnitude; here the cap is also floored at 0 and never passes `MAX_DECIMAL_PLACES`, the
    decoder's own ceiling, so every block the learner writes decodes. No scheme up to the cap
    gives `None`.
  - Resolution walks the units Python `datetime` can hold, coarsest first, and keeps the first
    one every value is a whole multiple of. Sub-microsecond data learns `microsecond`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

import numpy as np
import pandas as pd

from decoy_engine.internal.pandas_compat import utc_naive_datetimes
from decoy_engine.quality.snapshot import finite_numeric_cells

MAX_DECIMAL_PLACES: Final = 15

# Microseconds per unit, coarsest first.
_UNIT_MICROS: Final = (
    ("day", 86_400_000_000),
    ("hour", 3_600_000_000),
    ("minute", 60_000_000),
    ("second", 1_000_000),
    ("millisecond", 1_000),
)
RESOLUTIONS: Final = frozenset(name for name, _ in _UNIT_MICROS) | {"microsecond"}


def _is_places(value: object) -> bool:
    # `type(...) is int` on purpose: a bool, float or numeric string is refused, not coerced.
    return value is None or (type(value) is int and 0 <= value <= MAX_DECIMAL_PLACES)


def _is_resolution(value: object) -> bool:
    return type(value) is str and value in RESOLUTIONS


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


# The one descriptor: kind -> member -> exact-type check. Categorical and empty carry `{}`.
CELL_FORMAT_MEMBERS: Final[Mapping[str, Mapping[str, Callable[[object], bool]]]] = {
    "numeric": {"decimal_places": _is_places},
    "datetime": {"resolution": _is_resolution},
    "freetext": {"blank_count": _is_count},
    "categorical": {},
    "empty": {},
}


def cell_format_member_ok(kind: str, member: str, value: object) -> bool:
    """True when `member` is allowed on `kind` and `value` has its declared exact type."""
    check = CELL_FORMAT_MEMBERS.get(kind, {}).get(member)
    return check is not None and check(value)


def scrub_cell_format(kind: str, block: object) -> dict[str, Any] | None:
    """The block with every unknown or mistyped member nulled; a non-dict block becomes `None`.

    Default-deny for the redacted fit: a hand-edited block cannot use an allowed key to carry a
    source value.
    """
    if not isinstance(block, dict):
        return None
    return {k: (v if cell_format_member_ok(kind, k, v) else None) for k, v in block.items()}


# ── learn ────────────────────────────────────────────────────────────────────


def _source_float(series: pd.Series) -> np.dtype[Any]:
    """The float width the source stored, so rounding is judged in that width.

    A float32 1.1 widened to float64 reads as 1.100000023841858; judged in float64 it has no
    short decimal form. Nullable and Arrow dtypes expose their numpy width as `numpy_dtype`.
    """
    try:
        native = np.dtype(getattr(series.dtype, "numpy_dtype", series.dtype))
    except TypeError:
        return np.dtype(np.float64)
    return native if native.kind == "f" else np.dtype(np.float64)


def _decimal_places(non_null: pd.Series) -> int | None:
    width = _source_float(non_null)
    cells = finite_numeric_cells(non_null).astype(width)
    if cells.size == 0:
        return None
    if (cells % 1 == 0).all():
        return 0
    top = float(np.abs(cells).max())
    digits = int(np.floor(np.log10(top))) + 1 if top >= 1 else 0
    cap = min(MAX_DECIMAL_PLACES, max(0, np.finfo(width).precision - digits))
    for places in range(cap + 1):
        if (cells == np.round(cells, places)).all():
            return places
    return None


def _resolution(non_null: pd.Series) -> str:
    stamps = utc_naive_datetimes(non_null)
    if len(stamps) == 0 or (stamps.dt.nanosecond != 0).any():
        return "microsecond"
    micros = stamps.to_numpy(dtype="datetime64[us]").astype("int64")
    for name, size in _UNIT_MICROS:
        if (micros % size == 0).all():
            return name
    return "microsecond"


def learn_cell_format(non_null: pd.Series, kind: str) -> dict[str, Any]:
    """The `cell_format` block for one column's non-null cells, by snapshot kind."""
    if kind == "numeric":
        return {"decimal_places": _decimal_places(non_null)}
    if kind == "datetime":
        return {"resolution": _resolution(non_null)}
    if kind == "freetext":
        return {"blank_count": int((non_null.astype(str) == "").sum())}
    return {}


def add_cell_format(columns: dict[str, dict[str, Any]], df: pd.DataFrame) -> None:
    """Attach a `cell_format` block to every column entry of a snapshot."""
    labels = {str(c): c for c in df.columns}
    for name, entry in columns.items():
        entry["cell_format"] = learn_cell_format(df[labels[name]].dropna(), entry["kind"])

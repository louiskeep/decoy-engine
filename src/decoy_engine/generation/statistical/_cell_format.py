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

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, NoReturn

import numpy as np
import pandas as pd

from decoy_engine import reasons
from decoy_engine.internal.pandas_compat import utc_naive_datetimes
from decoy_engine.quality.snapshot import finite_numeric_cells

if TYPE_CHECKING:
    import pyarrow as pa

    from decoy_engine.generation.statistical._spec import StatisticalSpec

MAX_DECIMAL_PLACES: Final = 15
# Whole floats beyond this lose integer spacing, so they stay floats instead of becoming int64.
_INT_SAFE: Final = 2**53

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


# ── read ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CellFormat:
    """What the sampler needs from a fitted `cell_format` block, validated.

    `null_count` and `row_count` come from the snapshot column entry (the rate is
    `null_count / row_count`); `apply_nulls` is False when the column sets `null_probability`,
    so one null mechanism acts on a column.
    """

    null_count: int
    row_count: int
    apply_nulls: bool
    decimal_places: int | None = None
    resolution: str | None = None
    blank_count: int = 0


def _invalid(column: object, member: str, why: str) -> NoReturn:
    # Lazy: `_spec` imports this module for `CellFormat`. The message names the member, never its value.
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    raise StatisticalSpecError(
        code=reasons.plan.STATISTICAL_CELL_FORMAT_INVALID,
        message=f"statistical column {column!r}: cell_format {member}: {why}.",
    )


def _check_counts(column: object, null_count: object, row_count: object) -> None:
    if type(null_count) is not int or type(row_count) is not int:
        _invalid(column, "null_count", "counts must be integers")
    if not 0 <= null_count <= row_count:
        _invalid(column, "null_count", "must lie between 0 and the row count")


def _check_members(column: object, kind: str, members: Mapping[str, object]) -> None:
    for member, value in members.items():
        if not cell_format_member_ok(kind, member, value):
            _invalid(
                column,
                str(member) if member in _ALL_MEMBERS else "block",
                "unsupported member or type",
            )


_ALL_MEMBERS: Final = frozenset(m for ms in CELL_FORMAT_MEMBERS.values() for m in ms)


def _check_blank_count(
    column: object, blank_count: int, non_null_count: int, first_edge: object, first_bin: object
) -> None:
    if blank_count > non_null_count:
        _invalid(column, "blank_count", "exceeds the non-null cells")
    if blank_count == 0:
        return
    if first_edge != 0 or not isinstance(first_bin, (int, float)) or blank_count > first_bin:
        _invalid(column, "blank_count", "does not fit the first length bin")


def decode_cell_format(
    column: object,
    kind: str,
    entry: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    apply_nulls: bool,
) -> CellFormat | None:
    """The validated `CellFormat` of a snapshot column entry, or None when it has no block.

    Every refusal is `plan.statistical_cell_format_invalid` naming the column and the member.
    """
    if "cell_format" not in entry:
        return None
    block = entry["cell_format"]
    if type(block) is not dict:
        _invalid(column, "block", "must be an object")
    if isinstance(snapshot.get("dp"), Mapping):
        _invalid(column, "block", "not allowed on a differentially private snapshot")
    _check_members(column, kind, block)
    null_count, non_null = entry.get("null_count"), entry.get("non_null_count")
    if type(null_count) is not int or type(non_null) is not int or null_count < 0 or non_null < 0:
        _invalid(column, "null_count", "counts must be non-negative integers")
    row_count = null_count + non_null
    if row_count != snapshot.get("row_count"):
        _invalid(column, "null_count", "counts disagree with the snapshot row count")
    blank_count = block.get("blank_count", 0)
    if kind == "freetext":
        stats = entry.get("stats") or {}
        edges = stats.get("length_bin_edges") or [None]
        counts = stats.get("length_bin_counts") or [None]
        _check_blank_count(column, blank_count, non_null, edges[0], counts[0])
    return CellFormat(
        null_count=null_count,
        row_count=row_count,
        apply_nulls=apply_nulls,
        decimal_places=block.get("decimal_places"),
        # A hand-written datetime block without a resolution means "no flooring".
        resolution=block.get("resolution", "microsecond" if kind == "datetime" else None),
        blank_count=blank_count,
    )


_FIELDS: Final = frozenset(CellFormat.__dataclass_fields__)


def cell_format_from_dict(column: object, kind: str, data: object) -> CellFormat | None:
    """Rebuild a `CellFormat` from its stored six-field form with the same rules as the snapshot reader."""
    if data is None:
        return None
    if not isinstance(data, Mapping) or set(data) != _FIELDS:
        _invalid(column, "block", "must be an object with exactly the stored members")
    _check_counts(column, data["null_count"], data["row_count"])
    if type(data["apply_nulls"]) is not bool:
        _invalid(column, "apply_nulls", "must be a boolean")
    places, resolution, blanks = data["decimal_places"], data["resolution"], data["blank_count"]
    stored = {"decimal_places": places, "resolution": resolution, "blank_count": blanks}
    for member, value in stored.items():
        allowed = member in CELL_FORMAT_MEMBERS.get(kind, {})
        unused = value is None if member != "blank_count" else value == 0
        if not (cell_format_member_ok(kind, member, value) if allowed else unused):
            _invalid(column, member, "unsupported member or type")
    if blanks > data["row_count"] - data["null_count"]:
        _invalid(column, "blank_count", "exceeds the non-null cells")
    return CellFormat(
        null_count=data["null_count"],
        row_count=data["row_count"],
        apply_nulls=data["apply_nulls"],
        decimal_places=places,
        resolution=resolution,
        blank_count=blanks,
    )


# ── apply ────────────────────────────────────────────────────────────────────


def round_numeric(x: float, places: int) -> float:
    """Round half to even (Python `round`), the same rule as pandas/numpy `round` at fit."""
    return round(x, places)


def _grid_inside(x: float, places: int, low: float, high: float) -> float:
    """`x` rounded to `places`, moved to the nearest grid point inside [low, high] when it fell outside."""
    r = round_numeric(x, places)
    scale = 10.0**places
    if r < low:
        r = round_numeric(math.ceil(low * scale) / scale, places)
    elif r > high:
        r = round_numeric(math.floor(high * scale) / scale, places)
    return min(max(r, low), high)


def format_numeric(x: float, fmt: CellFormat, low: float, high: float, *, as_int: bool) -> Any:
    """A drawn value in the source's decimal places, inside [low, high]; `int` when `as_int`."""
    places = fmt.decimal_places
    if places is None:
        return int(min(max(round(x), low), high)) if as_int else x
    r = _grid_inside(x, places, low, high)
    return int(r) if as_int else r


def emits_int(spec: StatisticalSpec) -> bool:
    """True when a numeric column is written as Python `int` (Arrow int64)."""
    if is_integer_dtype(spec.dtype):
        return True
    fmt = spec.cell_format
    if fmt is None or fmt.decimal_places != 0:
        return False
    edges = spec.stats.get("bin_edges") or [0]
    return float(edges[0]) >= -_INT_SAFE and float(edges[-1]) <= _INT_SAFE


def is_integer_dtype(dtype: str) -> bool:
    return dtype.lstrip("uU").startswith("int") or dtype.startswith("Int")


_FLOOR_MICROS: Final = dict(_UNIT_MICROS)
_ONE_MICRO: Final = timedelta(microseconds=1)


def floor_datetime(ts: datetime, resolution: str, low: datetime) -> datetime | date:
    """`ts` floored to the source resolution and kept at or above `low`; a `date` at day resolution."""
    size = _FLOOR_MICROS.get(resolution)
    if size is not None:
        micros = (ts - datetime(1970, 1, 1)) // _ONE_MICRO
        ts = datetime(1970, 1, 1) + (micros - micros % size) * _ONE_MICRO
        ts = max(ts, low)
    return ts.date() if resolution == "day" else ts


def cell_format_arrow_type(spec: StatisticalSpec) -> pa.DataType:
    """The Arrow type of a fitted column, from the spec so an all-None slice keeps it."""
    import pyarrow as pa

    kind = spec.kind
    if kind == "numeric":
        return pa.int64() if emits_int(spec) else pa.float64()
    if kind == "datetime":
        return (
            pa.date32()
            if spec.cell_format and spec.cell_format.resolution == "day"
            else pa.timestamp("us")
        )
    if kind == "categorical":
        from decoy_engine.generation.statistical._sample import _categorical_tables

        values, _ = _categorical_tables(spec)
        return pa.array(values).type if values else pa.null()
    if kind == "freetext":
        return pa.string()
    return pa.null()

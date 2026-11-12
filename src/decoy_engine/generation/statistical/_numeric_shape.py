"""Shape of a fitted number column beyond its histogram: a quantile grid or exact values.

The equal-width histogram a snapshot always carries lets one extreme value, a skewed column or a
column with few levels spread generated numbers across empty range. A fit therefore adds one
optional block under a numeric column's `stats`, and generation prefers it to the histogram:

- `quantile_grid` (`probs`, `values`): sampled by inverse CDF over the piecewise-linear empirical
  quantile function. Hyndman and Fan, "Sample Quantiles in Statistical Packages", The American
  Statistician 50(4), 1996, type 7 (numpy `quantile(method="linear")`); scikit-learn
  `QuantileTransformer` stores the same grid; inversion is Devroye, Non-Uniform Random Variate
  Generation, 1986, ch. II.2.
- `exact_values` (`values`, `counts`, `dropped_count`) for a column with at most
  `CATEGORICAL_DISTINCT_CAP` distinct values: a weighted draw of the observed values, as synthpop
  `syn(minnumlevels=)` and DataSynthesizer's categorical threshold treat a numeric with few levels.

Release floor. `MIN_RELEASE_COUNT` (K) is the minimum source-row mass, after the minimum-frequency
rule of statistical disclosure control (Hundepool et al., Statistical Disclosure Control, Wiley
2012, section 4.2). In grid mode every interval between grid points and each tail beyond the ends
holds at least K rows, and no value outside [K-th smallest, K-th largest] is emitted. An interior
grid point may equal a source value held by fewer than K rows, as the plain snapshot's `p05`,
`p50`, `p95` and `bin_edges` already can; a redacted fit nulls both blocks. In exact mode every
stored value is held by at least K rows.

This module holds both halves, as `_cell_format.py` does: the fit (`add_numeric_shape`) and the
read (`decode_numeric_shape`, `numeric_shape_withheld`, `grid_value`). No new snapshot version:
the keys are additive, an old snapshot has neither and generates as before.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final, NoReturn

import numpy as np
import pandas as pd

from decoy_engine import reasons
from decoy_engine.profiling import MIN_RELEASE_COUNT
from decoy_engine.quality import CATEGORICAL_DISTINCT_CAP
from decoy_engine.quality.snapshot import finite_numeric_cells

GRID_KEY: Final = "quantile_grid"
EXACT_KEY: Final = "exact_values"
_GRID_MEMBERS: Final = frozenset({"probs", "values"})
_EXACT_MEMBERS: Final = frozenset({"values", "counts", "dropped_count"})

# Fewer finite cells than this get no grid: the K-row tails would leave under nine points.
MIN_GRID_ROWS: Final = 100
# Largest share of finite cells exact mode may leave out as rare values.
MAX_DROPPED_SHARE: Final = 0.01
_EPS: Final = 1e-9
# Ladder in thousandths: 0.001, 0.005, 0.01..0.99 by 0.01, 0.995, 0.999. Integers so no float
# step accumulates.
_LADDER_MILLI: Final = (1, 5, *range(10, 1000, 10), 995, 999)


# -- fit ---------------------------------------------------------------------


def _grid_probs(n: int) -> list[float]:
    """Probabilities where every adjacent interval and each tail holds at least K of `n` rows."""
    k = MIN_RELEASE_COUNT
    tail = max(0.001, k / n)
    upper = 1 - tail
    probs = [tail]
    for milli in _LADDER_MILLI:
        p = milli / 1000
        if tail < p < upper and (p - probs[-1]) * n >= k - _EPS and (upper - p) * n >= k - _EPS:
            probs.append(p)
    probs.append(upper)
    return probs


def _quantile_grid(
    finite: np.ndarray[Any, np.dtype[np.float64]], low: float, high: float
) -> dict[str, Any]:
    probs = _grid_probs(int(finite.size))
    raw = np.quantile(finite, probs, method="linear")
    # `bin_edges` are rounded by the snapshot; the clamp only ever moves a value toward the
    # interior, so it stays inside the K-th extremes and inside the edges the decoder checks.
    return {"probs": probs, "values": [float(min(max(v, low), high)) for v in raw]}


def _exact_values(cells: pd.Series, n: int) -> dict[str, Any] | None:
    """Observed values held by at least K rows, or None when more than 1% of rows are rare."""
    counts = cells.value_counts(sort=False).sort_index()
    kept = counts[counts >= MIN_RELEASE_COUNT]
    dropped = int(counts.sum() - kept.sum())
    if kept.empty or dropped > MAX_DROPPED_SHARE * n:
        return None
    as_int = pd.api.types.is_integer_dtype(cells)
    return {
        "values": [int(v) if as_int else float(v) for v in kept.index],
        "counts": [int(c) for c in kept],
        "dropped_count": dropped,
    }


def _finite_cells(series: pd.Series) -> pd.Series:
    """The cells in their source dtype (an int64 never passes through float) minus nan and inf."""
    as_float = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    return series[np.isfinite(as_float)]


def _fit_column(stats: dict[str, Any], series: pd.Series) -> None:
    edges = stats.get("bin_edges")
    finite = finite_numeric_cells(series)
    n = int(finite.size)
    if not edges or n == 0:
        return
    cells = _finite_cells(series)
    if cells.nunique() <= CATEGORICAL_DISTINCT_CAP:
        exact = _exact_values(cells, n)
        if exact is not None:
            stats[EXACT_KEY] = exact
            return
    if n >= MIN_GRID_ROWS:
        stats[GRID_KEY] = _quantile_grid(finite, float(edges[0]), float(edges[-1]))


def add_numeric_shape(columns: dict[str, dict[str, Any]], df: pd.DataFrame) -> None:
    """Attach `exact_values` or `quantile_grid` to each numeric column entry of a snapshot."""
    labels = {str(c): c for c in df.columns}
    for name, entry in columns.items():
        if entry.get("kind") == "numeric" and isinstance(entry.get("stats"), dict):
            _fit_column(entry["stats"], df[labels[name]].dropna())


# -- read --------------------------------------------------------------------


@dataclass(frozen=True)
class NumericShape:
    """What the sampler draws from: a grid (`counts is None`) or exact values and their counts."""

    probs: tuple[float, ...]
    values: tuple[float | int, ...]
    counts: tuple[int, ...] | None


def _invalid(column: object, member: str, why: str) -> NoReturn:
    # Lazy: `_spec` imports this module. The message names the column, member and counts only.
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    raise StatisticalSpecError(
        code=reasons.plan.STATISTICAL_STATS_DEGENERATE,
        message=f"statistical column {column!r}: {member}: {why}.",
    ) from None


def _finite_float(value: object) -> float | None:
    """`value` as a finite float; None for a bool, a string or an int float cannot hold."""
    if type(value) not in (int, float):
        return None
    as_float = math.nan
    overflow = False
    try:
        as_float = float(value)  # type: ignore[arg-type]
    except OverflowError:
        overflow = True
    # Raised by the caller, after this block has closed, so no context rides along.
    return None if overflow or not math.isfinite(as_float) else as_float


def _members(column: object, key: str, block: object, expected: frozenset[str]) -> dict[str, Any]:
    if type(block) is not dict:
        _invalid(column, key, "the block is not an object")
    if block.keys() != expected:
        _invalid(column, key, "the block members are not exactly " + ", ".join(sorted(expected)))
    return block


def _member_list(column: object, key: str, member: str, raw: object) -> list[Any]:
    if type(raw) is not list:
        _invalid(column, f"{key}.{member}", "not a list")
    return raw


def _floats(column: object, member: str, raw: list[Any]) -> list[float]:
    out: list[float] = []
    for item in raw:
        as_float = _finite_float(item)
        if as_float is None:
            _invalid(column, member, "an entry is not a finite number")
        out.append(as_float)
    return out


def _edges(column: object, stats: Mapping[str, Any]) -> tuple[float, float]:
    raw = stats.get("bin_edges")
    if type(raw) is list and len(raw) >= 2:
        low, high = _finite_float(raw[0]), _finite_float(raw[-1])
        if low is not None and high is not None:
            return low, high
    _invalid(column, "bin_edges", "needed to check the numeric shape and not usable")


# Largest fitted row count a numeric shape may claim (about 1.1 trillion rows). Above it the
# float slack in the grid floor check would cover whole rows and the exact-mode weights would
# stop being float-safe, so a larger claimed mass is a hand-edited snapshot and is refused.
_MAX_MASS = 2**40


def _mass(column: object, stats: Mapping[str, Any]) -> int:
    """Finite row count of the column: the sum of its validated `bin_counts`."""
    raw = stats.get("bin_counts")
    if type(raw) is not list or not raw:
        _invalid(column, "bin_counts", "needed to check the numeric shape and not usable")
    if any(type(c) is not int or c < 0 for c in raw):
        _invalid(column, "bin_counts", "a count is not a non-negative integer")
    total = sum(raw)
    if total > _MAX_MASS:
        _invalid(column, "bin_counts", f"the counts add up to more than {_MAX_MASS} rows")
    return total


def _check_grid_mass(column: object, probs: list[float], n: int) -> None:
    """Each adjacent interval and both tails must hold at least K of the `n` fitted rows."""
    gaps = [probs[0], 1.0 - probs[-1], *(b - a for a, b in pairwise(probs))]
    slack = _EPS + n * 2**-50
    if any(g * n < MIN_RELEASE_COUNT - slack for g in gaps):
        _invalid(
            column,
            f"{GRID_KEY}.probs",
            f"an interval or tail holds under {MIN_RELEASE_COUNT} of the {n} fitted rows",
        )


def _decode_grid(column: object, block: object, edges: tuple[float, float], n: int) -> NumericShape:
    members = _members(column, GRID_KEY, block, _GRID_MEMBERS)
    raw_probs = _member_list(column, GRID_KEY, "probs", members["probs"])
    raw_values = _member_list(column, GRID_KEY, "values", members["values"])
    if len(raw_probs) != len(raw_values):
        _invalid(
            column,
            GRID_KEY,
            f"probs and values differ in length ({len(raw_probs)}, {len(raw_values)})",
        )
    if len(raw_probs) < 2:
        _invalid(column, GRID_KEY, f"needs at least 2 points, has {len(raw_probs)}")
    probs = _floats(column, f"{GRID_KEY}.probs", raw_probs)
    values = _floats(column, f"{GRID_KEY}.values", raw_values)
    if any(not 0.0 <= p <= 1.0 for p in probs):
        _invalid(column, f"{GRID_KEY}.probs", "a probability is outside [0, 1]")
    if any(not edges[0] <= v <= edges[1] for v in values):
        _invalid(column, f"{GRID_KEY}.values", "a value is outside bin_edges")
    if any(b <= a for a, b in pairwise(probs)):
        _invalid(column, f"{GRID_KEY}.probs", "not strictly increasing")
    if any(b < a for a, b in pairwise(values)):
        _invalid(column, f"{GRID_KEY}.values", "decreasing")
    _check_grid_mass(column, probs, n)
    return NumericShape(tuple(probs), tuple(values), None)


def _tolerance(edge: float) -> float:
    # Half the 12-decimal rounding unit of the snapshot plus one float unit at the edge.
    return 5e-13 + abs(edge) * 2**-52


def _decode_exact(
    column: object, block: object, edges: tuple[float, float], n: int
) -> NumericShape:
    members = _members(column, EXACT_KEY, block, _EXACT_MEMBERS)
    raw_values = _member_list(column, EXACT_KEY, "values", members["values"])
    raw_counts = _member_list(column, EXACT_KEY, "counts", members["counts"])
    dropped = members["dropped_count"]
    if not raw_values or len(raw_values) != len(raw_counts):
        _invalid(
            column,
            EXACT_KEY,
            f"values and counts are empty or differ in length ({len(raw_values)}, {len(raw_counts)})",
        )
    if type(dropped) is not int or dropped < 0:
        _invalid(column, f"{EXACT_KEY}.dropped_count", "not a non-negative integer")
    if any(type(c) is not int or c < MIN_RELEASE_COUNT for c in raw_counts):
        _invalid(
            column,
            f"{EXACT_KEY}.counts",
            f"a count is not an integer of at least {MIN_RELEASE_COUNT}",
        )
    if sum(raw_counts) + dropped != n:
        _invalid(
            column, EXACT_KEY, f"counts and dropped_count do not add up to the {n} fitted rows"
        )
    floats = _floats(column, f"{EXACT_KEY}.values", raw_values)
    low, high = edges[0] - _tolerance(edges[0]), edges[1] + _tolerance(edges[1])
    if any(not low <= f <= high for f in floats):
        _invalid(column, f"{EXACT_KEY}.values", "a value is outside bin_edges")
    if any(b <= a for a, b in pairwise(raw_values)):
        _invalid(column, f"{EXACT_KEY}.values", "not strictly ascending")
    return NumericShape((), tuple(raw_values), tuple(raw_counts))


def redacted_fix_hint(*, numeric: bool) -> str:
    """The advice appended to a redacted-snapshot refusal. Numeric shapes need no opt-in flag."""
    if numeric:
        return (
            "Use a faker or pattern generator for this column, or re-fit with "
            "`--include-values` (the snapshot then holds the number shape)."
        )
    return (
        "Use a faker or pattern generator for this column, or re-fit with "
        "`--include-values` and set `allow_real_categories: true` (the snapshot then "
        "holds real values)."
    )


def numeric_shape_withheld(stats: Mapping[str, Any]) -> bool:
    """True when either block holds a non-null value (a redacted fit leaves both None)."""
    return stats.get(GRID_KEY) is not None or stats.get(EXACT_KEY) is not None


def decode_numeric_shape(
    column: object, stats: Mapping[str, Any], carrier: str | None
) -> NumericShape | None:
    """The validated grid or exact table of a numeric column, or None for the histogram path.

    A DP-verified column (`carrier` set) ignores both keys. Raises StatisticalSpecError with
    `plan.statistical_stats_degenerate` for a malformed block, naming the column and member only.
    """
    if carrier is not None:
        return None
    grid, exact = stats.get(GRID_KEY), stats.get(EXACT_KEY)
    if grid is None and exact is None:
        return None
    if grid is not None and exact is not None:
        _invalid(column, "numeric shape", f"{GRID_KEY} and {EXACT_KEY} are both present")
    edges = _edges(column, stats)
    n = _mass(column, stats)
    if grid is not None:
        return _decode_grid(column, grid, edges, n)
    return _decode_exact(column, exact, edges, n)


def grid_value(probs: Sequence[float], values: Sequence[float], u: float) -> float:
    """Inverse CDF of the piecewise-linear grid at `u` in [0, 1).

    The first and last probability count as 0 and 1, so the tail mass beyond the ends is drawn
    inside the outer intervals and no value past the ends is made.
    """
    last = len(probs) - 1
    j = bisect_right(probs, u, 1, last) - 1
    p_lo = 0.0 if j == 0 else probs[j]
    p_hi = 1.0 if j + 1 == last else probs[j + 1]
    x = values[j] + (u - p_lo) / (p_hi - p_lo) * (values[j + 1] - values[j])
    return min(max(x, values[0]), values[-1])

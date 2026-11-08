"""The distribution snapshot `decoy fit` writes, with value-sensitive columns' cells withheld.

Which columns are sensitive, which stats are cell values and how the marker is written all come
from `_value_redaction.py`, the same helpers a scan document uses. This module only applies the
rules that need the data: re-binning a numeric column and bounding a datetime by years.

Pattern: statistical disclosure control global recoding; HIPAA Safe Harbor year-only dates.

Numeric columns are released as one coarse interval (statistical disclosure control global
recoding, Hundepool et al., Statistical Disclosure Control, Wiley 2012, sections 3.4.2 to
3.4.3; R sdcMicro `globalRecode`). The bounds sit on a power-of-ten grid one step above the
column's range and strictly outside every cell, so they say the range's order of magnitude and
nothing else; there is no interior edge because any edge can equal a real cell. Datetimes keep
their per-year counts and are bounded by the years around them (HIPAA Safe Harbor,
45 CFR 164.514(b)(2)(i)(C)). Pretty-number axis bounds follow Heckbert, "Nice numbers for graph
labels" (Graphics Gems, 1990), in the powers-of-ten form.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Any, Final

import numpy as np
import pandas as pd

from decoy_engine.profiling._value_redaction import (
    DATA_RULES,
    Rule,
    _apply,
    snapshot_keeper,
    snapshot_rules,
    stamp_value_policy,
    value_sensitive_names,
)
from decoy_engine.profiling.profiler import profile_columns
from decoy_engine.quality.snapshot import (
    _DEFAULT_CATEGORICAL_TOP_K,
    _DEFAULT_CONTINGENCY_TOP_K,
    _DEFAULT_NUMERIC_BINS,
    compute_distribution_snapshot,
    numeric_stats,
)

# The snapshot writes 12 decimal places, so a finer grid step could not be written.
_FLOOR: Final = Decimal(1).scaleb(-12)
_PRECISION: Final = 80


def _grid_edges(lo: float, hi: float) -> tuple[Decimal, Decimal]:
    """Bounds on a power-of-ten grid strictly outside `[lo, hi]`, in exact decimal arithmetic.

    Float arithmetic put a real extreme back on the grid for about 7% of decimal-grid inputs,
    so everything starts from the written float's repr. The step exceeds the range, so each
    extreme is known only to within a step wider than the range itself.
    """
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        d_lo, d_hi = Decimal(repr(lo)), Decimal(repr(hi))
        span = d_hi - d_lo if d_hi > d_lo else max(abs(d_lo), Decimal(1))
        mag = max(abs(d_lo), abs(d_hi), Decimal(1))
        step = max(
            Decimal(1).scaleb(span.adjusted() + 1),
            Decimal(1).scaleb(mag.adjusted() - 12),
            _FLOOR,
        )
        lower = (d_lo / step).to_integral_value(ROUND_FLOOR) * step - step
        upper = (d_hi / step).to_integral_value(ROUND_FLOOR) * step + 2 * step
    return lower, upper


def _fallback_edge(cells: np.ndarray) -> float:
    """First half-integer B in 1.5 .. N+1.5 with neither -B nor B equal to a cell.

    N cells cannot cover N+1 absolute candidates, so one exists, and it is a function of the
    count and which candidates the data touches, not of any other cell.
    """
    n = cells.size
    offsets = np.abs(cells) - 1.5
    hit = (offsets >= 0) & (offsets <= n) & (offsets == np.floor(offsets))
    taken = set(offsets[hit].astype(np.int64).tolist())
    k = next(i for i in range(n + 1) if i not in taken)
    return k + 1.5


def _coarse_domain(lo: float, hi: float, cells: np.ndarray) -> tuple[float, float]:
    """The one-bin domain for a numeric column: grid bounds, or the finite fallback near float max."""
    lower, upper = _grid_edges(lo, hi)
    try:
        flo, fhi = float(lower), float(upper)
    except OverflowError:
        flo, fhi = math.inf, math.inf
    if math.isfinite(flo) and math.isfinite(fhi) and flo < cells.min() and fhi > cells.max():
        return flo, fhi
    edge = _fallback_edge(cells)
    return -edge, edge


def _finite_cells(non_null: pd.Series) -> np.ndarray:
    arr = pd.to_numeric(non_null, errors="coerce").dropna().to_numpy(dtype=float)
    return arr[np.isfinite(arr)]


def _coarse_range(non_null: pd.Series, stats: dict[str, Any], keys: Collection[str]) -> None:
    if stats.get("min") is None:
        return  # no finite cell, so nothing here is a value
    cells = _finite_cells(non_null)
    domain = _coarse_domain(stats["min"], stats["max"], cells)
    coarse = numeric_stats(non_null, bins=1, domain=domain)
    for key in keys:
        stats[key] = coarse[key]


def _year_edge(rule: Rule, stats: dict[str, Any]) -> str | None:
    years = [b["year"] for b in stats.get("year_bins", [])]
    if not years:
        return None
    if rule is Rule.PREVIOUS_YEAR_START:
        return f"{min(years) - 1:04d}-01-01T00:00:00"
    return f"{max(years) + 1:04d}-12-31T23:59:59"


def _collapse_counted_items(container: dict[str, Any], item_key: str) -> None:
    """Move the counts of a value-keyed list into `other_count`, so the row mass survives its labels."""
    items = container.get(item_key)
    if isinstance(items, list):
        container["other_count"] = container.get("other_count", 0) + sum(
            item["count"] for item in items
        )


def _withhold(container: dict[str, Any], scope: str) -> None:
    """Aggregate then empty the value containers of `scope`, and null any key the registry lacks."""
    rules = snapshot_rules(scope)
    for key, rule in rules.items():
        if rule is Rule.EMPTY_LIST:
            _collapse_counted_items(container, key)
    applied = {k: r for k, r in rules.items() if r not in DATA_RULES}
    _apply(container, applied, snapshot_keeper(scope), denied_value=None)


def _redact_column(non_null: pd.Series, col: dict[str, Any]) -> None:
    _apply(col, {}, snapshot_keeper("column"), denied_value=None)
    kind, stats = col["kind"], col["stats"]
    data_keys = {k: r for k, r in snapshot_rules(kind).items() if r in DATA_RULES}
    coarse = [k for k, r in data_keys.items() if r is Rule.COARSE_RANGE]
    if coarse:
        _coarse_range(non_null, stats, coarse)
    for key, rule in data_keys.items():
        if rule is not Rule.COARSE_RANGE and key in stats:
            stats[key] = _year_edge(rule, stats)
    _withhold(stats, kind)


def distribution_snapshot_document(
    df: pd.DataFrame,
    *,
    source_label: str = "<frame>",
    include_values: bool = False,
    joint_columns: Sequence[tuple[str, str]] | None = None,
    numeric_bins: int = _DEFAULT_NUMERIC_BINS,
    categorical_top_k: int = _DEFAULT_CATEGORICAL_TOP_K,
    contingency_top_k: int = _DEFAULT_CONTINGENCY_TOP_K,
    high_cardinality_columns: Collection[str] = (),
    profile_row_cap: int | None = None,
) -> dict[str, Any]:
    """The snapshot JSON to write for `df`: the raw snapshot with sensitive columns' values withheld.

    `compute_distribution_snapshot` stays raw for in-memory callers. `include_values=True`
    keeps every value and adds a `raw` policy marker. `profile_row_cap` limits the rows the
    sensitivity profile sees, the way `decoy scan` samples.
    """
    if type(include_values) is not bool:
        raise TypeError(f"include_values must be a bool, not {type(include_values).__name__}")
    doc = compute_distribution_snapshot(
        df,
        joint_columns=joint_columns,
        numeric_bins=numeric_bins,
        categorical_top_k=categorical_top_k,
        contingency_top_k=contingency_top_k,
        high_cardinality_columns=high_cardinality_columns,
    )
    profiled = df.head(profile_row_cap) if profile_row_cap else df
    profile = profile_columns(profiled, source_label)
    names = value_sensitive_names(profile.to_dict()["fields"])
    if not include_values:
        sensitive = set(names)
        labels = {str(c): c for c in df.columns}
        for name, col in doc["columns"].items():
            if name in sensitive:
                _redact_column(df[labels[name]].dropna(), col)
        for joint in doc["joints"]:
            if sensitive & set(joint["columns"]):
                _withhold(joint, "joint")
    return stamp_value_policy(doc, names, include_values=include_values)

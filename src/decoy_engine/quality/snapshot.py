"""Distribution snapshot: per-column + per-joint summary of a DataFrame.

V2 Phase 3 Distribution Integrity, Sprint D1a (Measurement Foundation).

This module is the measurement primitive that the rest of D1 stacks on:
D1b (diagnostic) and D1c (fidelity) both consume snapshots without
re-walking the underlying frames. The snapshot is the only thing that
crosses the "input data -> downstream metrics" boundary.

Hard requirements (enforced by tests):
  - Deterministic: same input + same kwargs -> byte-identical JSON.
  - JSON-serializable: `json.dumps(snapshot)` succeeds without custom
    encoders. No numpy scalars, no Timestamp, no Decimal, no NaN/Inf.
  - Pure: never mutates the input frame.

Prior art surveyed (per repo rule "use established methodology"):
  - pandas describe / quantile / value_counts / crosstab: the per-column
    summary stats and pairwise contingency are thin wrappers around these
    primitives rather than hand-rolled accumulators.
  - numpy histogram_bin_edges with the 'auto' rule was considered but
    rejected: equal-width bins with a fixed N are easier to compare
    across two frames (source vs output) because the bin boundaries are
    a function of the data range, not the data distribution.
  - SDV (Synthetic Data Vault) TabularPreset metadata pattern: each
    column has a kind ("numerical" / "categorical" / "datetime" / "text"
    in SDV; we use "numeric" / "categorical" / "datetime" / "freetext" /
    "empty") that drives which stats apply. Snapshot kind is recorded
    explicitly so D1c can refuse to compare across mismatched kinds.
  - NIST SP 800-188 Sec. 4 (Trustworthy De-identification): utility
    metrics for de-identified data are typically a comparison of
    marginal distributions and pairwise contingency tables. Snapshot
    captures exactly enough to support those comparisons later without
    re-touching raw rows.

What this module does NOT do (out of scope for D1a, owned by D1b/c/d):
  - Compute fidelity scores or grades. That belongs in D1c (fidelity.py)
    and operates on two snapshots, not two frames.
  - Diagnostic structural checks (column survival, dtype drift). That
    belongs in D1b (diagnostic.py).
  - Persist snapshots to a job record. That's D2.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.internal.pandas_compat import canonical_dtype_label

DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION = "distribution-snapshot/v1"

# Determinism guard. Floating-point quantile / mean / std calculations can
# vary in the last few bits across BLAS builds; rounding to 12 places
# eliminates that without losing meaningful precision for downstream
# fidelity scoring (which tolerates several percent drift).
_FLOAT_PRECISION = 12

# Fallback bin count when the caller does not override. 10 matches the
# storm/profiler histogram so the snapshot and the existing per-column
# profile visualization line up at the bin level.
_DEFAULT_NUMERIC_BINS = 10

# Top-K caps. Categorical columns past the cap collapse into "other_count";
# joint contingency tables past the cap do the same. Keep these modest:
# the snapshot is for evidence, not for analysis, and the JSON size needs
# to fit in a job-record column at D2 time.
_DEFAULT_CATEGORICAL_TOP_K = 20
_DEFAULT_CONTINGENCY_TOP_K = 25

# Cardinality cap that splits "categorical" from "freetext" for object
# columns. Mirrors storm/profiler._CATEGORICAL_DISTINCT_CAP (30) so the
# kind assignment is consistent across the engine.
_CATEGORICAL_DISTINCT_CAP = 30


def compute_distribution_snapshot(
    df: pd.DataFrame,
    *,
    joint_columns: Sequence[tuple[str, str]] | None = None,
    numeric_bins: int = _DEFAULT_NUMERIC_BINS,
    categorical_top_k: int = _DEFAULT_CATEGORICAL_TOP_K,
    contingency_top_k: int = _DEFAULT_CONTINGENCY_TOP_K,
) -> dict[str, Any]:
    """Compute a deterministic, JSON-serializable distribution snapshot.

    Args:
        df: Input frame. Not mutated.
        joint_columns: Pairs of column names whose pairwise contingency
            table should be captured. Pairs referencing unknown columns
            are silently skipped (the snapshot is not a validator).
        numeric_bins: Number of equal-width bins per numeric column.
        categorical_top_k: Max distinct values kept per categorical
            column; rest collapse into the column's `other_count`.
        contingency_top_k: Max cells kept per joint table; rest collapse
            into the joint's `other_count`.

    Returns:
        A dict matching schema `distribution-snapshot/v1`. See module
        docstring for shape contract.
    """
    columns_block: dict[str, dict[str, Any]] = {}
    for col in df.columns:
        columns_block[str(col)] = _column_snapshot(
            df[col],
            numeric_bins=numeric_bins,
            categorical_top_k=categorical_top_k,
        )

    joints_block: list[dict[str, Any]] = []
    if joint_columns:
        for raw_pair in joint_columns:
            pair = _normalize_joint_pair(raw_pair, df.columns)
            if pair is None:
                continue
            joints_block.append(
                _joint_snapshot(df, pair[0], pair[1], top_k=contingency_top_k),
            )

    return {
        "schema_version": DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
        "row_count": len(df),
        "columns": columns_block,
        "joints": joints_block,
    }


# ── per-column ───────────────────────────────────────────────────────────────


def _column_snapshot(
    series: pd.Series,
    *,
    numeric_bins: int,
    categorical_top_k: int,
) -> dict[str, Any]:
    non_null = series.dropna()
    null_count = int(series.isna().sum())
    non_null_count = len(non_null)
    distinct_count = int(non_null.nunique()) if non_null_count else 0
    # Audit M5: stable label across pandas majors -- the snapshot digest
    # is a USER-HELD baseline; pandas-3's 'str' label must not invalidate
    # digests minted under pandas 2.x. See internal.pandas_compat.
    dtype = canonical_dtype_label(series.dtype)

    if non_null_count == 0:
        return {
            "dtype": dtype,
            "kind": "empty",
            "null_count": null_count,
            "non_null_count": 0,
            "distinct_count": 0,
            "stats": {},
        }

    kind, stats = _stats_for(non_null, numeric_bins=numeric_bins, top_k=categorical_top_k)
    return {
        "dtype": dtype,
        "kind": kind,
        "null_count": null_count,
        "non_null_count": non_null_count,
        "distinct_count": distinct_count,
        "stats": stats,
    }


def _stats_for(
    non_null: pd.Series,
    *,
    numeric_bins: int,
    top_k: int,
) -> tuple[str, dict[str, Any]]:
    if pd.api.types.is_bool_dtype(non_null):
        return "categorical", _categorical_stats(non_null.astype(str), top_k=top_k)
    if pd.api.types.is_numeric_dtype(non_null):
        return "numeric", _numeric_stats(non_null, bins=numeric_bins)
    if pd.api.types.is_datetime64_any_dtype(non_null):
        return "datetime", _datetime_stats(non_null)

    distinct = non_null.nunique()
    if distinct <= _CATEGORICAL_DISTINCT_CAP:
        return "categorical", _categorical_stats(non_null.astype(str), top_k=top_k)
    return "freetext", _freetext_stats(non_null.astype(str), bins=numeric_bins)


def _numeric_stats(non_null: pd.Series, *, bins: int) -> dict[str, Any]:
    arr = pd.to_numeric(non_null, errors="coerce").dropna().to_numpy(dtype=float)
    # to_numpy + dropna handles object columns of stringified numbers
    # without surprising the histogram math below.
    if arr.size == 0:
        return {}
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        # All values were inf / -inf / nan. Report an empty numeric stat
        # rather than crashing; JSON cannot encode inf.
        return {
            "min": None,
            "max": None,
            "mean": None,
            "std": None,
            "quantiles": {},
            "bin_edges": [],
            "bin_counts": [],
        }
    lo = float(finite.min())
    hi = float(finite.max())
    mean = float(finite.mean())
    # std with ddof=0 matches pandas describe()'s "population" semantics
    # only when explicitly requested; we use ddof=1 to align with
    # pandas defaults and storm/profiler precedent.
    std = float(finite.std(ddof=1)) if finite.size > 1 else 0.0

    quantiles_idx = [0.05, 0.25, 0.50, 0.75, 0.95]
    q_vals = np.quantile(finite, quantiles_idx)
    quantiles = {
        f"p{round(p * 100):02d}": _round(float(v))
        for p, v in zip(quantiles_idx, q_vals, strict=True)
    }

    if lo == hi:
        # Zero-range fallback: single bin covering the constant value.
        bin_edges = [lo, hi]
        bin_counts = [int(finite.size)]
    else:
        counts, edges = np.histogram(finite, bins=bins, range=(lo, hi))
        bin_edges = [_round(float(e)) for e in edges]
        bin_counts = [int(c) for c in counts]

    return {
        "min": _round(lo),
        "max": _round(hi),
        "mean": _round(mean),
        "std": _round(std),
        "quantiles": quantiles,
        "bin_edges": bin_edges,
        "bin_counts": bin_counts,
    }


def _categorical_stats(non_null: pd.Series, *, top_k: int) -> dict[str, Any]:
    counts = non_null.value_counts()
    # value_counts already orders by count desc, but ties are broken in
    # observation order. Re-sort by (-count, str(value)) for a stable
    # deterministic ordering across runs.
    sorted_items = sorted(
        ((str(val), int(cnt)) for val, cnt in counts.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    head = sorted_items[:top_k]
    tail = sorted_items[top_k:]
    other_count = int(sum(cnt for _, cnt in tail))
    return {
        "top_values": [{"value": val, "count": cnt} for val, cnt in head],
        "other_count": other_count,
    }


def _datetime_stats(non_null: pd.Series) -> dict[str, Any]:
    coerced = pd.to_datetime(non_null, errors="coerce").dropna()
    if len(coerced) == 0:
        return {}
    # Strip timezone before isoformat so two snapshots taken on machines
    # in different zones don't diverge on the string representation.
    if getattr(coerced.dtype, "tz", None) is not None:
        coerced = coerced.dt.tz_convert("UTC").dt.tz_localize(None)
    by_year = coerced.dt.year.value_counts().sort_index()
    year_bins = [{"year": int(year), "count": int(cnt)} for year, cnt in by_year.items()]
    return {
        "min": coerced.min().isoformat(),
        "max": coerced.max().isoformat(),
        "year_bins": year_bins,
    }


def _freetext_stats(non_null: pd.Series, *, bins: int) -> dict[str, Any]:
    lens = non_null.astype(str).str.len().to_numpy(dtype=int)
    if lens.size == 0:
        return {}
    lo = int(lens.min())
    hi = int(lens.max())
    mean = float(lens.mean())
    std = float(lens.std(ddof=1)) if lens.size > 1 else 0.0
    if lo == hi:
        bin_edges = [lo, hi]
        bin_counts = [int(lens.size)]
    else:
        counts, edges = np.histogram(lens, bins=bins, range=(lo, hi))
        bin_edges = [round(float(e)) for e in edges]
        bin_counts = [int(c) for c in counts]
    return {
        "length": {
            "min": lo,
            "max": hi,
            "mean": _round(mean),
            "std": _round(std),
        },
        "length_bin_edges": bin_edges,
        "length_bin_counts": bin_counts,
    }


# ── per-joint ────────────────────────────────────────────────────────────────


def _normalize_joint_pair(
    raw: tuple[str, str],
    columns: pd.Index,
) -> tuple[str, str] | None:
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        return None
    a, b = str(raw[0]), str(raw[1])
    if a == b or a not in columns or b not in columns:
        return None
    # Sort the pair so (a,b) and (b,a) collapse to the same joint entry.
    return (a, b) if a < b else (b, a)


def _joint_snapshot(
    df: pd.DataFrame,
    col_a: str,
    col_b: str,
    *,
    top_k: int,
) -> dict[str, Any]:
    sub = df[[col_a, col_b]].dropna()
    if len(sub) == 0:
        return {
            "columns": [col_a, col_b],
            "cell_count": 0,
            "cells": [],
            "other_count": 0,
        }
    # Cast to str so heterogeneous types in the joint key don't break
    # JSON serialization. Snapshots are for shape comparison, not exact
    # value preservation; the original raw values stay in the source
    # frame.
    a_vals = sub[col_a].astype(str)
    b_vals = sub[col_b].astype(str)
    ct = pd.crosstab(a_vals, b_vals)
    cells: list[dict[str, Any]] = []
    for a_val in ct.index:
        for b_val in ct.columns:
            count = int(ct.at[a_val, b_val])
            if count == 0:
                continue
            cells.append({"key": [str(a_val), str(b_val)], "count": count})
    cells.sort(key=lambda c: (-c["count"], c["key"][0], c["key"][1]))
    head = cells[:top_k]
    tail = cells[top_k:]
    other = int(sum(c["count"] for c in tail))
    return {
        "columns": [col_a, col_b],
        "cell_count": len(cells),
        "cells": head,
        "other_count": other,
    }


# ── helpers ──────────────────────────────────────────────────────────────────


def _round(value: float) -> float:
    """Round to the snapshot's float precision; return 0.0 for non-finite.

    JSON does not encode NaN / +-Inf. Callers feed only finite values
    here (the non-finite filter sits in _numeric_stats) but the round
    helper keeps the guard local to avoid future regressions.
    """
    if not math.isfinite(value):
        return 0.0
    return round(value, _FLOAT_PRECISION)

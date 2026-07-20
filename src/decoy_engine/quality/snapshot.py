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

Sensitive artifact: a categorical column's `top_values` already carries
REAL source values (gated by the consumer-side `allow_real_categories`
opt-in, generation/statistical/_spec.py). A `high_cardinality_columns`
entry (HC-5) retains the FULL observed vocabulary for that column with
no top-K collapse, so the artifact additionally exposes every distinct
code AND rare-code presence/absence -- treat it with the same care as
a raw extract of that column.

DPS-1 (`numeric_domains` / `dp_mode`, dp.py's sibling half of the
marginal-DP effort): a Laplace-noised histogram over a DATA-DERIVED
range is not actually DP -- real min/max leak regardless of noise on
the counts. `numeric_domains` declares a fixed range (SmartNoise/OpenDP
pattern); `dp_mode` fails closed without one. Out-of-domain values
clamp (Dwork & Roth Sec. 3.3) rather than vanish from `row_count`.

Gate remediation Fix 1 (P0): `dp_mode` also bypasses `categorical_top_k`
SELECTION (itself data-dependent, Korolova et al. WWW 2009) -- every
observed label becomes a tau-threshold candidate, unlike
`high_cardinality_columns` (full vocabulary, no threshold).

Gate remediation Fix 2 (P0): `dp_mode` rejects datetime/freetext columns
outright (data-dependent support, no caller override).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.internal.pandas_compat import canonical_dtype_label

DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION = "distribution-snapshot/v1"


class DistributionSnapshotError(Exception):
    """Fit-time `high_cardinality` contract violation. Machine-readable code.

    Raised in place of silently degrading: a column marked
    `high_cardinality` promises FULL-fidelity vocabulary retention, so a
    request this module cannot honor (wrong dtype, or a vocabulary/label
    size the JSON artifact should not carry) fails the fit loudly instead
    of falling back to the ordinary top-K/freetext behavior.
    """

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


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
# columns. Mirrors storm/_distributions._STORM_CATEGORICAL_DISTINCT_CAP (30)
# so the kind assignment is consistent across the engine. This is the SINGLE
# fit source of truth (HC-5 D4): the STORM copy is a deliberately independent
# profiler-chart constant, not derived from this one.
_CATEGORICAL_DISTINCT_CAP = 30

# HC-5 hard safety limits for an opt-in `high_cardinality` column: the "full
# fidelity" promise (retain every observed category) must fail loudly rather
# than silently truncate or balloon the JSON artifact. Both are typed fit
# errors, never a silent cap.
_HIGH_CARDINALITY_MAX_DISTINCT = 100_000
_HIGH_CARDINALITY_MAX_LABEL_BYTES = 16 * 1024 * 1024  # 16 MiB


def compute_distribution_snapshot(
    df: pd.DataFrame,
    *,
    joint_columns: Sequence[tuple[str, str]] | None = None,
    numeric_bins: int = _DEFAULT_NUMERIC_BINS,
    categorical_top_k: int = _DEFAULT_CATEGORICAL_TOP_K,
    contingency_top_k: int = _DEFAULT_CONTINGENCY_TOP_K,
    high_cardinality_columns: Collection[str] = (),
    numeric_domains: dict[str, tuple[float, float]] | None = None,
    dp_mode: bool = False,
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
        high_cardinality_columns: Column names (HC-5) to fit with FULL
            vocabulary retention instead of the ordinary cardinality cliff
            (>30 distinct -> freetext) and top-K collapse: forces
            `kind: categorical` and keeps every observed category
            (`other_count` stays 0). Opt-in only -- omitted (the default)
            means byte-identical output to every prior engine version.
            Restricted to string/object/category source dtype and to
            <=100,000 distinct values / <=16 MiB combined UTF-8 label
            bytes; violating either raises `DistributionSnapshotError`
            rather than silently degrading. Unknown column names are
            silently skipped (matching `joint_columns`, not a validator).
        numeric_domains: DPS-1 (module docstring). `{column: (lo, hi)}`;
            bin_edges/min/max derive from the (clamped) domain when set.
        dp_mode: DPS-1. Fail-closed: numeric requires `numeric_domains`;
            datetime/freetext are rejected outright (Fix 2); categorical
            bypasses `categorical_top_k` (Fix 1, module docstring).

    Returns:
        A dict matching schema `distribution-snapshot/v1` (module
        docstring). `dp_mode`/`numeric_domains` adds a per-column
        `support_origin` key ("data"|"caller"), else omitted (byte-
        identical to every prior engine version).

    Raises:
        DistributionSnapshotError: bad `high_cardinality_columns` shape
            or safety-limit violation (see that arg's docs above).
        ValueError: `dp_mode=True` with a numeric column that has no
            `numeric_domains` entry, or with any datetime/freetext column.
    """
    # MED-3: a bare str satisfies `Collection[str]` structurally, so
    # `high_cardinality_columns="code"` would silently iterate characters
    # ({"c","o","d","e"}) instead of naming the "code" column -- the real
    # column then stays freetext with no error. Reject the shape explicitly
    # before it can silently disable the feature.
    if isinstance(high_cardinality_columns, (str, bytes)):
        raise DistributionSnapshotError(
            code="high_cardinality_columns_not_collection",
            message=(
                "high_cardinality_columns must be a collection of column names "
                "(e.g. a list/set/tuple), not a bare string. Pass ['code'], not 'code'."
            ),
        )
    high_cardinality_set = frozenset(str(c) for c in high_cardinality_columns)
    numeric_domains = numeric_domains or {}
    # support_origin appears only when DPS-1 params are used (byte-identity).
    emit_support_origin = dp_mode or bool(numeric_domains)
    columns_block: dict[str, dict[str, Any]] = {}
    for col in df.columns:
        columns_block[str(col)] = _column_snapshot(
            df[col],
            numeric_bins=numeric_bins,
            categorical_top_k=categorical_top_k,
            high_cardinality=str(col) in high_cardinality_set,
            numeric_domain=numeric_domains.get(str(col)),
            dp_mode=dp_mode,
            emit_support_origin=emit_support_origin,
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
    high_cardinality: bool = False,
    numeric_domain: tuple[float, float] | None = None,
    dp_mode: bool = False,
    emit_support_origin: bool = False,
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
        kind, support_origin = "empty", "data"
        stats: dict[str, Any] = {}
    else:
        kind, stats, support_origin = _stats_for(
            non_null,
            numeric_bins=numeric_bins,
            top_k=categorical_top_k,
            high_cardinality=high_cardinality,
            numeric_domain=numeric_domain,
            dp_mode=dp_mode,
        )
    out: dict[str, Any] = {
        "dtype": dtype,
        "kind": kind,
        "null_count": null_count,
        "non_null_count": non_null_count,
        "distinct_count": distinct_count,
        "stats": stats,
    }
    if emit_support_origin:
        out["support_origin"] = support_origin
    return out


def _stats_for(
    non_null: pd.Series,
    *,
    numeric_bins: int,
    top_k: int,
    high_cardinality: bool = False,
    numeric_domain: tuple[float, float] | None = None,
    dp_mode: bool = False,
) -> tuple[str, dict[str, Any], str]:
    if high_cardinality:
        return "categorical", _high_cardinality_categorical_stats(non_null), "data"
    # Fix 1: dp_mode bypasses top-K candidate SELECTION (data-dependent).
    # Fix 7: a categorical column whose candidacy was made data-independent
    # (full observed vocabulary) is marked support_origin="full_vocabulary",
    # parallel to numeric's "caller" -- the consume-side provenance check
    # (plan._checks_dp) requires it, so a top-K-truncated (non-DP) fit
    # cannot masquerade as DP under a dp-declared pipeline.
    categorical_top_k = None if dp_mode else top_k
    cat_support = "full_vocabulary" if dp_mode else "data"
    if pd.api.types.is_bool_dtype(non_null):
        stats = _categorical_stats(non_null.astype(str), top_k=categorical_top_k)
        return "categorical", stats, cat_support
    if pd.api.types.is_numeric_dtype(non_null):
        if dp_mode and numeric_domain is None:
            raise ValueError(
                f"dp_mode requires a data-independent numeric_domain for column {non_null.name!r}."
            )
        stats = _numeric_stats(non_null, bins=numeric_bins, domain=numeric_domain)
        return "numeric", stats, ("caller" if numeric_domain is not None else "data")
    if pd.api.types.is_datetime64_any_dtype(non_null):
        if dp_mode:  # Fix 2: year_bins derive from the real observed year set.
            _raise_dp_mode_unsupported_kind(non_null.name, "datetime", "year_bins")
        return "datetime", _datetime_stats(non_null), "data"

    distinct = non_null.nunique()
    if distinct <= _CATEGORICAL_DISTINCT_CAP:
        stats = _categorical_stats(non_null.astype(str), top_k=categorical_top_k)
        return "categorical", stats, cat_support
    if dp_mode:  # Fix 2: length min/max derive from the real observed lens.
        _raise_dp_mode_unsupported_kind(non_null.name, "freetext", "length bin edges")
    return "freetext", _freetext_stats(non_null.astype(str), bins=numeric_bins), "data"


def _raise_dp_mode_unsupported_kind(column: Any, kind: str, support_field: str) -> None:
    """Fix 2: datetime/freetext support is data-dependent, no caller
    override (unlike numeric's `numeric_domain`); fail closed pre-stats."""
    raise ValueError(
        f"dp_mode does not support {kind} column {column!r}: {support_field} derive from "
        "the real observed data (data-dependent support), which DPS-1 does not make "
        "data-independent. Mask or exclude this column from the dp_mode fit, or fit it "
        "outside dp_mode (its release will not carry a DP guarantee)."
    )


def _high_cardinality_categorical_stats(non_null: pd.Series) -> dict[str, Any]:
    """Force-categorical, full-vocabulary path for a `high_cardinality`
    column (HC-5 D1): bypasses the 30-distinct cliff AND the top-K collapse
    (`top_k=None` in `_categorical_stats` retains every observed value, so
    `other_count` is always 0 here). Restricted to string/object/category
    source dtype -- numeric-looking codes (NDC, some ICD variants) must be
    loaded as strings to preserve leading zeros, so a numeric/datetime/bool
    source dtype is a typed error rather than a silent int coercion.

    Dennis-LOW-1: the dtype gate is an ALLOW-list (object / pandas string /
    category only), not a deny-list -- a deny-list of bool/numeric/datetime
    lets timedelta/period/interval dtypes fall through uncaught. Reject
    everything not explicitly allowed, by construction.
    """
    name = non_null.name
    dtype = non_null.dtype
    is_allowed = (
        pd.api.types.is_object_dtype(dtype)
        or isinstance(dtype, pd.CategoricalDtype)
        or (pd.api.types.is_string_dtype(dtype) and not pd.api.types.is_object_dtype(dtype))
    )
    if not is_allowed:
        raise DistributionSnapshotError(
            code="high_cardinality_non_string_dtype",
            message=(
                f"high_cardinality column {name!r}: source dtype "
                f"{canonical_dtype_label(dtype)!r} is not string/object/category. "
                f"Load it as a string column to preserve leading zeros and other "
                f"code formatting before fitting."
            ),
        )
    str_vals = non_null.astype(str)
    distinct = str_vals.unique()
    # MED-2 (gate remediation): string coercion must be a faithful 1:1
    # relabeling of the source categories, or category membership and mass are
    # silently rewritten. Comparing distinct COUNTS is not enough: a
    # simultaneous merge + split leaves the counts equal while scrambling the
    # partition (e.g. object [1, 1.0, "1"] -- pandas treats 1 and 1.0 as one
    # raw value, but str() splits them into "1"/"1.0", so 2 raw == 2 labels yet
    # the mapping is not a bijection). Verify the row partition induced by raw
    # values equals the one induced by string labels: the count of distinct
    # (raw, label) pairs must equal both the raw-class count and the label
    # count. Any many-to-one (merge) or one-to-many (split) fails loud.
    # `non_null` has nulls dropped upstream, so factorize's NaN handling is
    # moot; default args keep this compatible with the full supported pandas
    # range (the `use_na_sentinel` kwarg only exists from pandas 1.5).
    raw_codes = pd.factorize(non_null)[0]
    str_codes = pd.factorize(str_vals)[0]
    n_raw = len(set(raw_codes.tolist()))
    n_pairs = len(set(zip(raw_codes.tolist(), str_codes.tolist(), strict=True)))
    if n_pairs != n_raw or n_pairs != len(distinct):
        raise DistributionSnapshotError(
            code="high_cardinality_ambiguous_string_coercion",
            message=(
                f"high_cardinality column {name!r}: string coercion does not "
                f"preserve the source categories 1:1 ({n_raw} source values, "
                f"{len(distinct)} string labels, {n_pairs} distinct pairings); some "
                f"categories merge or split after coercion. Normalize the column "
                f"to a single consistent type before fitting."
            ),
        )
    if len(distinct) > _HIGH_CARDINALITY_MAX_DISTINCT:
        raise DistributionSnapshotError(
            code="high_cardinality_distinct_limit_exceeded",
            message=(
                f"high_cardinality column {name!r}: {len(distinct)} distinct values "
                f"exceeds the {_HIGH_CARDINALITY_MAX_DISTINCT} safety limit for full "
                f"vocabulary retention."
            ),
        )
    # Codex-LOW: a lone surrogate (or other unencodable label) must raise
    # the module's typed error boundary, not a raw UnicodeEncodeError. The
    # offending label is not included in the message -- it may be
    # unprintable or sensitive; the column name is enough to act on.
    try:
        label_bytes = sum(len(v.encode("utf-8")) for v in distinct)
    except UnicodeEncodeError as exc:
        raise DistributionSnapshotError(
            code="high_cardinality_invalid_label_encoding",
            message=(
                f"high_cardinality column {name!r}: a category label could not be "
                f"encoded as UTF-8 (e.g. a lone surrogate). Normalize the column's "
                f"text encoding before fitting."
            ),
        ) from exc
    if label_bytes > _HIGH_CARDINALITY_MAX_LABEL_BYTES:
        raise DistributionSnapshotError(
            code="high_cardinality_label_bytes_limit_exceeded",
            message=(
                f"high_cardinality column {name!r}: {label_bytes} combined UTF-8 "
                f"category-label bytes exceeds the "
                f"{_HIGH_CARDINALITY_MAX_LABEL_BYTES} safety limit for full "
                f"vocabulary retention."
            ),
        )
    stats = _categorical_stats(str_vals, top_k=None)
    # HIGH-2 (gate remediation): provenance marker so a downstream consumer
    # (generation/statistical/_spec.py) can prove this column was actually
    # fit with full-vocabulary retention rather than trusting the
    # generate-side `high_cardinality: true` flag alone (which proves
    # nothing about how the artifact was fit). Additive-only -- never set
    # on the default `_categorical_stats` path, so ordinary snapshots stay
    # byte-identical to every prior engine version.
    stats["high_cardinality"] = True
    return stats


def _numeric_stats(
    non_null: pd.Series, *, bins: int, domain: tuple[float, float] | None = None
) -> dict[str, Any]:
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

    if domain is not None:
        # DPS-1: range from the caller's domain; clamp (not drop) outliers.
        lo, hi = float(domain[0]), float(domain[1])
        hist_input = np.clip(finite, lo, hi)
    else:
        lo = float(finite.min())
        hi = float(finite.max())
        hist_input = finite

    if lo == hi:
        # Zero-range fallback: single bin covering the constant value.
        bin_edges = [lo, hi]
        bin_counts = [int(finite.size)]
    else:
        counts, edges = np.histogram(hist_input, bins=bins, range=(lo, hi))
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


def _categorical_stats(non_null: pd.Series, *, top_k: int | None) -> dict[str, Any]:
    """`top_k=None` (HC-5 high_cardinality) retains every observed category:
    `other_count` is always 0 and the deterministic (-count, str(value))
    sort still applies, it just never gets truncated."""
    counts = non_null.value_counts()
    # value_counts already orders by count desc, but ties are broken in
    # observation order. Re-sort by (-count, str(value)) for a stable
    # deterministic ordering across runs.
    sorted_items = sorted(
        ((str(val), int(cnt)) for val, cnt in counts.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    if top_k is None:
        head, tail = sorted_items, []
    else:
        head, tail = sorted_items[:top_k], sorted_items[top_k:]
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

"""Fidelity comparison: marginal + pairwise similarity between two snapshots.

V2 Phase 3 Distribution Integrity, Sprint D1c.

Consumes two snapshots produced by
`decoy_engine.quality.snapshot.compute_distribution_snapshot` and
emits a per-column and per-joint similarity score in [0, 1] plus an
overall aggregate. Pure dict-in / dict-out, like D1b.

This is the layer that operators actually look at to answer "how
much shape did the masking job preserve?" The numbers are bounded
(1.0 = identical, 0.0 = completely disjoint), symmetric (swap source
and output and the score is unchanged), and per-column so the worst
columns surface immediately.

Method per kind:

  - numeric: normalized RMSE on the p05/p25/p50/p75/p95 quantile
    grid, similarity = 1 - min(1, RMSE / source_range). Quantile
    comparison is honest across independent histograms (which have
    different bin edges per snapshot); comparing bin counts directly
    would be apples-to-oranges because each snapshot's bins are
    equal-width on its own min/max.

  - categorical / bool: Total Variation Distance on the top-K
    proportion vectors plus the "other_count" lump. Similarity =
    1 - TVD. Keys present in only one side contribute their full
    probability mass to TVD (the standard half-sum-of-absolute-
    differences formula). This is the same metric SDV's
    `CategoricalCAP` and statsmodels `categorical similarity` use.

  - datetime: TVD on year_bins proportions, identical formula to the
    categorical case with the year as the categorical key.

  - freetext: normalized absolute difference of length mean, divided
    by the larger of the two `max` length values. Freetext fidelity
    is a weak signal anyway (the actual text content is meant to be
    transformed by hash / faker / redact); length preservation is
    enough to flag a strategy that, say, replaces every value with a
    single character.

  - empty: skipped from the marginal aggregate; recorded with
    `comparable: false`.

  - kind_mismatch: skipped from the aggregate; recorded with
    `comparable: false`. The diagnostic surfaces kind drift up
    front, so fidelity does not need to penalize twice.

Aggregation:

  - marginal: arithmetic mean of `comparable` column similarities.
    None if there were no comparable columns at all (e.g. every
    column is empty or kind-mismatched).
  - pairwise: arithmetic mean of `comparable` joint similarities.
    None if no joints overlap between snapshots.
  - overall_score: equal-weight mean of marginal and pairwise. None
    if both are None. Single-side present passes through.

Equal weighting is the SDV `QualityReport` default and the most
defensible choice without strategy-aware tuning (D4).

Prior art (per established-methodology rule):

  - SDV `QualityReport` uses KSComplement for numeric and TVComplement
    for categorical, then averages.
  - NIST SP 800-188 sec. 4 covers utility metrics for de-identified
    data; recommends presenting marginal + pairwise separately rather
    than rolling into one number.
  - Total Variation Distance is the symmetric f-divergence on
    discrete distributions:
        TVD(P, Q) = 0.5 * sum |P(x) - Q(x)|
    bounded in [0, 1], easy to interpret as "fraction of mass that
    would have to move from P's distribution to recover Q's".

A2 (2026-09): additive goodness-of-fit extras. Each numeric column entry
gains `extra_metrics.ks_complement` (a binned empirical-CDF approximation
of KSComplement) and each categorical / bool column entry gains
`extra_metrics.chi_cramers_v` (a common-partition two-sample chi-square,
normalized to Cramer's V). Both are named nested fields alongside the
existing `similarity` / `method` / `comparable` -- they do not replace or
feed the primary similarity, `overall_score`, or the grade. See
`_distribution_gof.py` for the formulas, source patterns
(`scipy.stats.ks_2samp` / `scipy.stats.chisquare`), and guards.

DI1 (2026-10): the GOF extras are extended to the remaining comparable
column classes so the measurement report does not silently cover only a
subset of types. Both reuse the EXISTING two-sample KS-complement method
(`ks_complement_binned`); no new statistic is introduced.

  - datetime gains `extra_metrics.ks_complement` computed on the yearly
    empirical CDF: the snapshot's `year_bins` are the ordinal binned
    numeric representation the primary datetime similarity already uses, so
    the year counts are re-expressed as a contiguous unit-width (one bin
    per calendar year) histogram and handed to `ks_complement_binned`. KS
    on the yearly ECDF is the natural ordinal two-sample GOF; it respects
    the temporal ordering a categorical chi-square would discard. Source
    pattern: `scipy.stats.ks_2samp` / SDV `QualityReport` KSComplement
    (same prior art as the numeric case, applied to the year grid).
  - freetext gains `extra_metrics.ks_complement` computed on the string
    LENGTH distribution, never the raw text: the snapshot already derives a
    `length_bin_edges` / `length_bin_counts` histogram (the same length
    signal the primary `length_mean_diff` similarity summarizes), so it
    feeds `ks_complement_binned` directly. Source pattern: identical to the
    numeric KS case.

A class that cannot support the method with the data on hand (e.g. a
freetext snapshot predating the length histogram, or a datetime column
with no year bins) does NOT fabricate a statistic: `ks_complement_binned`
returns its coded `comparable: false` + `reason` marker, which the report
surfaces as a `gof_unavailable` warning. `empty` and kind-mismatched
columns are still never given a GOF entry.

Out of scope (later sub-sprints own these):
  - Grade letter (A / B / C / D / F): D1d report assembly owns the
    mapping from score to grade.
  - Per-strategy expected-preservation bands: D4 policy.
  - Drift warnings that gate the job: D4 policy.
  - Folding the A2 extras into overall_score / grade: additive-only by
    design (plan open question 1); a separate, deliberate decision.
"""

from __future__ import annotations

import math
from typing import Any

from decoy_engine.quality._distribution_gof import (
    chi_square_cramers_v,
    ks_complement_binned,
)

QUALITY_FIDELITY_SCHEMA_VERSION = "quality-fidelity/v1"

# Precision pin for round-trip determinism. Same rationale as the
# snapshot module's _FLOAT_PRECISION: BLAS / numpy versions can wobble
# the last few bits of the inputs, and we want the output JSON to be
# byte-stable across machines.
_SCORE_PRECISION = 6


def compute_fidelity(
    source_snapshot: dict[str, Any],
    output_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Compute marginal + pairwise similarity scores from two snapshots.

    Args:
        source_snapshot: Snapshot of the pre-mask / pre-generate frame.
        output_snapshot: Snapshot of the post-mask / post-generate frame.

    Returns:
        A dict matching schema `quality-fidelity/v1`:
            {
              "schema_version": "quality-fidelity/v1",
              "marginal": {"score": float | None, "columns": [...]},
              "pairwise": {"score": float | None, "joints": [...]},
              "overall_score": float | None,
            }
        Each column / joint entry carries `{column|columns,
        similarity, method, comparable}`. `comparable: false`
        excludes the entry from the aggregate score.
    """
    src_cols = source_snapshot.get("columns", {})
    out_cols = output_snapshot.get("columns", {})
    shared_columns = sorted(set(src_cols.keys()) & set(out_cols.keys()))

    column_results: list[dict[str, Any]] = []
    for col in shared_columns:
        result = _column_similarity(src_cols[col], out_cols[col])
        column_results.append({"column": col, **result})

    comparable_cols = [c for c in column_results if c["comparable"]]
    marginal_score = (
        round(
            sum(c["similarity"] for c in comparable_cols) / len(comparable_cols),
            _SCORE_PRECISION,
        )
        if comparable_cols
        else None
    )

    src_joints = source_snapshot.get("joints", [])
    out_joints_by_pair = {tuple(j.get("columns", [])): j for j in output_snapshot.get("joints", [])}
    joint_results: list[dict[str, Any]] = []
    for src_joint in src_joints:
        pair = tuple(src_joint.get("columns", []))
        out_joint = out_joints_by_pair.get(pair)
        if out_joint is None:
            joint_results.append(
                {
                    "columns": list(pair),
                    "similarity": None,
                    "method": "no_output_joint",
                    "comparable": False,
                },
            )
            continue
        result = _joint_similarity(src_joint, out_joint)
        joint_results.append({"columns": list(pair), **result})

    comparable_joints = [j for j in joint_results if j["comparable"]]
    pairwise_score = (
        round(
            sum(j["similarity"] for j in comparable_joints) / len(comparable_joints),
            _SCORE_PRECISION,
        )
        if comparable_joints
        else None
    )

    overall = _compute_overall(marginal_score, pairwise_score)

    return {
        "schema_version": QUALITY_FIDELITY_SCHEMA_VERSION,
        "marginal": {"score": marginal_score, "columns": column_results},
        "pairwise": {"score": pairwise_score, "joints": joint_results},
        "overall_score": overall,
    }


# ── A2 goodness-of-fit wiring ────────────────────────────────────────────────


def _finalize_gof(result: dict[str, Any]) -> dict[str, Any]:
    """Round every float field of a `_distribution_gof` result to the same
    precision pin the primary similarity scores use, so the JSON stays
    byte-stable. The metric functions return raw floats on purpose (single
    source of truth for the rounding pin lives here, not duplicated into
    the sibling module)."""
    return {
        key: round(value, _SCORE_PRECISION) if isinstance(value, float) else value
        for key, value in result.items()
    }


def _datetime_gof_inputs(
    src_stats: dict[str, Any],
    out_stats: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Adapt each side's datetime `year_bins` into the `{bin_edges,
    bin_counts}` shape `ks_complement_binned` reads, so the yearly ECDF KS
    reuses the numeric method unchanged. A side with no year bins yields an
    empty dict, which the KS primitive reports as `missing_histogram`
    (coded, honest -- never a fabricated statistic)."""
    return _year_histogram(src_stats), _year_histogram(out_stats)


def _year_histogram(stats: dict[str, Any]) -> dict[str, Any]:
    """`year_bins` (a list of `{year, count}`) -> a contiguous unit-width
    histogram, one bin per calendar year across the observed span, with
    zero-count bins for years with no mass. Each bin `[y, y + 1)` is a
    normal (non-degenerate) interval, so the ECDF ramps continuously within
    a year and jumps only across years."""
    bins = stats.get("year_bins") or []
    counts_by_year: dict[int, int] = {}
    for b in bins:
        try:
            counts_by_year[int(b["year"])] = int(b["count"])
        except (KeyError, TypeError, ValueError):
            return {}
    if not counts_by_year:
        return {}
    y_min, y_max = min(counts_by_year), max(counts_by_year)
    bin_edges = list(range(y_min, y_max + 2))
    bin_counts = [counts_by_year.get(y, 0) for y in range(y_min, y_max + 1)]
    return {"bin_edges": bin_edges, "bin_counts": bin_counts}


def _freetext_gof_inputs(
    src_stats: dict[str, Any],
    out_stats: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map each side's freetext LENGTH histogram
    (`length_bin_edges` / `length_bin_counts`) onto the generic
    `{bin_edges, bin_counts}` keys `ks_complement_binned` reads. A snapshot
    without a length histogram yields an empty dict -> coded
    `missing_histogram`, never a fabricated statistic. The raw text is never
    read; only the length distribution crosses into the GOF."""
    return _length_histogram(src_stats), _length_histogram(out_stats)


def _length_histogram(stats: dict[str, Any]) -> dict[str, Any]:
    edges = stats.get("length_bin_edges") or []
    counts = stats.get("length_bin_counts") or []
    if not edges or not counts:
        return {}
    return {"bin_edges": edges, "bin_counts": counts}


# ── per-kind comparators ────────────────────────────────────────────────────


def _column_similarity(
    src_col: dict[str, Any],
    out_col: dict[str, Any],
) -> dict[str, Any]:
    src_kind = src_col.get("kind")
    out_kind = out_col.get("kind")
    if src_kind != out_kind:
        return {
            "similarity": None,
            "method": "kind_mismatch",
            "comparable": False,
        }
    if src_kind == "empty":
        return {
            "similarity": None,
            "method": "empty",
            "comparable": False,
        }

    src_stats = src_col.get("stats", {})
    out_stats = out_col.get("stats", {})

    if src_kind == "numeric":
        result = _numeric_similarity(src_stats, out_stats)
        # Codex FINAL gate MEDIUM finding: ks_complement is computed from
        # ITS OWN inputs (bin_edges/bin_counts) independent of whether the
        # primary quantile-RMSE path found itself comparable. A DP
        # snapshot (quality/dp.py:406) always carries a histogram but
        # deliberately leaves quantiles empty, so the primary correctly
        # returns comparable:false ("no_quantiles") while KS still has
        # everything it needs -- gating extra_metrics on the primary's
        # early return would silently drop a metric that was reachable.
        result["extra_metrics"] = {
            "ks_complement": _finalize_gof(ks_complement_binned(src_stats, out_stats)),
        }
        return result
    if src_kind in ("categorical", "bool"):
        result = _categorical_similarity(src_stats, out_stats)
        result["extra_metrics"] = {
            "chi_cramers_v": _finalize_gof(chi_square_cramers_v(src_stats, out_stats)),
        }
        return result
    if src_kind == "datetime":
        result = _datetime_similarity(src_stats, out_stats)
        # DI1: KS-complement on the yearly ECDF (the ordinal binned-numeric
        # representation the primary datetime similarity already uses). Reuses
        # the numeric KS method; no new statistic. Computed from its own inputs
        # independently of the primary's comparability (same rationale as the
        # numeric branch above).
        result["extra_metrics"] = {
            "ks_complement": _finalize_gof(
                ks_complement_binned(*_datetime_gof_inputs(src_stats, out_stats))
            ),
        }
        return result
    if src_kind == "freetext":
        result = _freetext_similarity(src_stats, out_stats)
        # DI1: KS-complement on the string-LENGTH distribution (never the raw
        # text), reading the snapshot's own length histogram. Reuses the
        # numeric KS method; no new statistic.
        result["extra_metrics"] = {
            "ks_complement": _finalize_gof(
                ks_complement_binned(*_freetext_gof_inputs(src_stats, out_stats))
            ),
        }
        return result
    return {
        "similarity": None,
        "method": f"unknown_kind:{src_kind}",
        "comparable": False,
    }


def _numeric_similarity(
    src_stats: dict[str, Any],
    out_stats: dict[str, Any],
) -> dict[str, Any]:
    src_q = src_stats.get("quantiles") or {}
    out_q = out_stats.get("quantiles") or {}
    shared = sorted(set(src_q.keys()) & set(out_q.keys()))
    if not shared:
        return {
            "similarity": None,
            "method": "no_quantiles",
            "comparable": False,
        }
    src_min = src_stats.get("min")
    src_max = src_stats.get("max")
    if src_min is None or src_max is None:
        return {
            "similarity": None,
            "method": "no_range",
            "comparable": False,
        }
    # Avoid division by zero when source has zero range; the quantile
    # absolute differences still tell us if output drifted off the
    # constant value.
    src_range = max(float(src_max) - float(src_min), 1.0)
    diffs_sq = []
    for key in shared:
        diff = (float(src_q[key]) - float(out_q[key])) / src_range
        diffs_sq.append(diff * diff)
    rmse = math.sqrt(sum(diffs_sq) / len(diffs_sq))
    similarity = max(0.0, 1.0 - min(1.0, rmse))
    return {
        "similarity": round(similarity, _SCORE_PRECISION),
        "method": "quantile_rmse",
        "comparable": True,
    }


def _categorical_similarity(
    src_stats: dict[str, Any],
    out_stats: dict[str, Any],
) -> dict[str, Any]:
    src_items = src_stats.get("top_values", [])
    out_items = out_stats.get("top_values", [])
    src_other = int(src_stats.get("other_count", 0))
    out_other = int(out_stats.get("other_count", 0))
    src_total = sum(int(item["count"]) for item in src_items) + src_other
    out_total = sum(int(item["count"]) for item in out_items) + out_other
    if src_total == 0 or out_total == 0:
        return {
            "similarity": None,
            "method": "no_data",
            "comparable": False,
        }
    src_probs = {item["value"]: int(item["count"]) / src_total for item in src_items}
    out_probs = {item["value"]: int(item["count"]) / out_total for item in out_items}
    keys = set(src_probs) | set(out_probs)
    tvd = sum(abs(src_probs.get(k, 0.0) - out_probs.get(k, 0.0)) for k in keys)
    tvd += abs((src_other / src_total) - (out_other / out_total))
    tvd_normalized = 0.5 * tvd
    similarity = max(0.0, 1.0 - tvd_normalized)
    return {
        "similarity": round(similarity, _SCORE_PRECISION),
        "method": "tvd",
        "comparable": True,
    }


def _datetime_similarity(
    src_stats: dict[str, Any],
    out_stats: dict[str, Any],
) -> dict[str, Any]:
    src_bins = src_stats.get("year_bins", [])
    out_bins = out_stats.get("year_bins", [])
    src_total = sum(int(b["count"]) for b in src_bins)
    out_total = sum(int(b["count"]) for b in out_bins)
    if src_total == 0 or out_total == 0:
        return {
            "similarity": None,
            "method": "no_data",
            "comparable": False,
        }
    src_probs = {b["year"]: int(b["count"]) / src_total for b in src_bins}
    out_probs = {b["year"]: int(b["count"]) / out_total for b in out_bins}
    keys = set(src_probs) | set(out_probs)
    tvd = 0.5 * sum(abs(src_probs.get(k, 0.0) - out_probs.get(k, 0.0)) for k in keys)
    similarity = max(0.0, 1.0 - tvd)
    return {
        "similarity": round(similarity, _SCORE_PRECISION),
        "method": "tvd",
        "comparable": True,
    }


def _freetext_similarity(
    src_stats: dict[str, Any],
    out_stats: dict[str, Any],
) -> dict[str, Any]:
    src_len = src_stats.get("length", {})
    out_len = out_stats.get("length", {})
    src_mean = src_len.get("mean")
    out_mean = out_len.get("mean")
    if src_mean is None or out_mean is None:
        return {
            "similarity": None,
            "method": "no_length",
            "comparable": False,
        }
    # Normalize by the larger of the two max lengths so a wholesale
    # length collapse (e.g. hash replaces everything with a 64-char
    # digest) gets a meaningful penalty rather than being capped at
    # the source's own scale.
    scale = max(int(src_len.get("max", 0)), int(out_len.get("max", 0)), 1)
    diff = abs(float(src_mean) - float(out_mean)) / scale
    similarity = max(0.0, 1.0 - min(1.0, diff))
    return {
        "similarity": round(similarity, _SCORE_PRECISION),
        "method": "length_mean_diff",
        "comparable": True,
    }


# ── joint comparator ────────────────────────────────────────────────────────


def _joint_similarity(
    src_joint: dict[str, Any],
    out_joint: dict[str, Any],
) -> dict[str, Any]:
    src_cells = src_joint.get("cells", [])
    out_cells = out_joint.get("cells", [])
    src_other = int(src_joint.get("other_count", 0))
    out_other = int(out_joint.get("other_count", 0))
    src_total = sum(int(c["count"]) for c in src_cells) + src_other
    out_total = sum(int(c["count"]) for c in out_cells) + out_other
    if src_total == 0 or out_total == 0:
        return {
            "similarity": None,
            "method": "no_data",
            "comparable": False,
        }
    src_probs = {tuple(c["key"]): int(c["count"]) / src_total for c in src_cells}
    out_probs = {tuple(c["key"]): int(c["count"]) / out_total for c in out_cells}
    keys = set(src_probs) | set(out_probs)
    tvd = sum(abs(src_probs.get(k, 0.0) - out_probs.get(k, 0.0)) for k in keys)
    tvd += abs((src_other / src_total) - (out_other / out_total))
    tvd_normalized = 0.5 * tvd
    similarity = max(0.0, 1.0 - tvd_normalized)
    return {
        "similarity": round(similarity, _SCORE_PRECISION),
        "method": "tvd",
        "comparable": True,
    }


# ── aggregation ─────────────────────────────────────────────────────────────


def _compute_overall(
    marginal: float | None,
    pairwise: float | None,
) -> float | None:
    if marginal is None and pairwise is None:
        return None
    if pairwise is None:
        return marginal
    if marginal is None:
        return pairwise
    # Equal weighting matches SDV QualityReport default and avoids
    # baking in a tuning decision before D4 policy sprint exists.
    return round((marginal + pairwise) / 2.0, _SCORE_PRECISION)

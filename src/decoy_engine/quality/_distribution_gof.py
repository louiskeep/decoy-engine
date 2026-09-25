"""Goodness-of-fit metrics (A2): KS-complement + chi-square Cramer's V.

Distribution Integrity, roadmap "Ready to build" / NEXT SET OF WORK item 4.
Plan: `docs/plans/2026-09-23-distribution-integrity-ks-chisquare.md`
(Codex GO-WITH-CHANGES).

Additive companions to `fidelity.py`'s quantile-RMSE (numeric) and TVD
(categorical) scores. `fidelity.py` wires these into named nested fields
on the existing column entry (`extra_metrics.ks_complement`,
`extra_metrics.chi_cramers_v`); this module owns only the two pure metric
functions and never touches the primary `similarity` / `method` a column
entry already carries.

Aggregate-only, same as the rest of D1c: both functions take the numeric /
categorical `stats` sub-dicts `compute_distribution_snapshot` already
produced (histogram bin edges + counts; top-K label counts + other_count)
and never see a raw cell value. Pure dict-in / float-and-dict-out, no
mutation of the inputs.

Prior art (established-methodology rule): `scipy.stats.ks_2samp` is the
reference two-sample KS test and `scipy.stats.chisquare` the reference
goodness-of-fit chi-square; SDV `QualityReport` reports their complements
(`KSComplement`, `TVComplement`) as similarity scores. We hand-roll both
from the snapshot's aggregate vectors rather than depending on scipy: the
inputs are already-binned summaries (not raw samples), so a raw
`ks_2samp`/`chisquare` call is not even applicable, and the closed-form
formulas below are small enough that a runtime dependency buys nothing
but cross-BLAS-build float wobble the snapshot's precision pin exists to
avoid.

1. Numeric KS-complement (binned empirical-CDF approximation). Snapshots
   hold independently-binned histograms (each side's edges are a function
   of that side's own min/max), not raw samples, so this is NOT
   `scipy.stats.ks_2samp`. We build a binned empirical CDF per side (mass
   allocated uniformly within a bin -- the one documented mass-allocation
   rule; a degenerate zero-width bin, i.e. a constant column, is a point
   mass), evaluate both CDFs on the union of the two sides' bin edges (the
   set of points where either piecewise-linear CDF can change slope, so
   the max gap between them is attained at one of these points), and take
   `D = max |CDF_src - CDF_out|`. Reported as `ks_complement_binned_cdf`
   to keep the approximation unmistakable in the method tag: resolution is
   bounded by the snapshot's bin count (default 10), not by the raw
   sample size.
2. Categorical chi-square Cramer's V. Each side's snapshot keeps its own
   top-K label set plus an `other_count` lump for the tail, so a label
   present in one side's top-K and absent from the other's may still be
   sitting inside that other side's `other_count` -- reading it as a
   literal zero would fabricate a contingency-table cell the snapshot
   cannot support. We build a COMMON partition first: labels in the
   intersection of both top-K sets keep their own column; every other
   label (present in only one side's top-K) is folded into that side's
   own `other` bucket alongside its existing `other_count`, so the two
   sides are never compared on a label neither side can attest to for the
   other. On that shared partition we compute the two-sample (row =
   source vs. output) homogeneity chi-square statistic and normalize to
   Cramer's V (`V = sqrt(chi2 / (n * min(rows-1, cols-1)))`; with 2 rows
   this reduces to `sqrt(chi2 / n)` whenever there are >= 2 shared
   columns), which needs only the statistic, n, and table shape -- no
   chi-square CDF, so no p-value is available or claimed. Reported as
   `chi_square_common_partition_cramers_v`. A partition that collapses to
   a single column (no identifiable overlap and nothing left in either
   `other` bucket) cannot support a comparison and is `comparable: False`.

Both metrics are bounded in [0, 1] and symmetric under swapping source and
output by construction: `D` and the chi-square statistic are built from
`abs()` / squared differences over a partition that does not depend on
which side is labeled "source", so swapping the two dict arguments leaves
the value unchanged.

Guards: a missing/malformed histogram, a zero-mass side, or a partition
with fewer than 2 identifiable columns returns `comparable: False` with a
`reason` string, never a NaN or an exception. Callers are responsible for
rounding (the snapshot's `_SCORE_PRECISION` pin) since this module returns
raw floats.
"""

from __future__ import annotations

import math
from typing import Any, TypedDict


class GofResult(TypedDict, total=False):
    """Shared return shape for both metric functions.

    `value` is the bounded [0, 1] similarity/complement score;
    `comparable` gates whether a caller should use it. `reason` is only
    present when `comparable` is False. The metric-specific raw statistic
    (`d_statistic` for KS, `cramers_v` / `chi2_statistic` for chi-square)
    is only present when `comparable` is True.
    """

    value: float | None
    method: str
    comparable: bool
    reason: str


_KS_METHOD = "ks_complement_binned_cdf"
_CHI_METHOD = "chi_square_common_partition_cramers_v"


def _incomparable(method: str, reason: str) -> dict[str, Any]:
    return {"value": None, "method": method, "comparable": False, "reason": reason}


# ── numeric: KS-complement on binned CDFs ───────────────────────────────────


def ks_complement_binned(
    src_stats: dict[str, Any],
    out_stats: dict[str, Any],
) -> dict[str, Any]:
    """Binned-CDF KS-complement between two numeric snapshot stats dicts.

    Reads only `bin_edges` / `bin_counts` (never raw values). Returns a
    dict with `value` (`ks_complement = 1 - D`, `None` if incomparable),
    `d_statistic` (the raw max-gap statistic, present iff comparable),
    `method`, `comparable`, and `reason` (present iff not comparable).
    """
    src_edges = src_stats.get("bin_edges") or []
    src_counts = src_stats.get("bin_counts") or []
    out_edges = out_stats.get("bin_edges") or []
    out_counts = out_stats.get("bin_counts") or []

    if len(src_edges) < 2 or len(out_edges) < 2 or not src_counts or not out_counts:
        return _incomparable(_KS_METHOD, "missing_histogram")
    if len(src_edges) != len(src_counts) + 1 or len(out_edges) != len(out_counts) + 1:
        return _incomparable(_KS_METHOD, "malformed_histogram")

    src_total = sum(src_counts)
    out_total = sum(out_counts)
    if src_total <= 0 or out_total <= 0:
        return _incomparable(_KS_METHOD, "zero_mass")

    src_edges_f = [float(e) for e in src_edges]
    out_edges_f = [float(e) for e in out_edges]

    # Fixed reduction order (sorted union, ascending) for byte-stability:
    # float summation order must not depend on dict/set iteration order.
    breakpoints = sorted(set(src_edges_f) | set(out_edges_f))

    # A binned CDF is right-continuous (`_binned_cdf`'s degenerate-bin
    # branch includes a point mass's edge itself), so it can jump at a
    # point-mass edge. The true KS statistic is a supremum over ALL real
    # x, and for two right-continuous, piecewise-linear-between-jumps
    # functions that supremum is attained at a breakpoint approached from
    # EITHER side: evaluating only the right-continuous value at each
    # breakpoint (the old behavior) misses the gap approached from just
    # below a jump, which can be the larger one (Codex FINAL gate HIGH
    # finding; see test_ks_complement_constant_vs_spanning_bin_gap_before_
    # mass for a worked repro). So every breakpoint is checked twice: once
    # at its right-continuous value, once at its left limit.
    d_statistic = 0.0
    for x in breakpoints:
        cdf_src_right = _binned_cdf(x, src_edges_f, src_counts, src_total)
        cdf_out_right = _binned_cdf(x, out_edges_f, out_counts, out_total)
        gap_right = abs(cdf_src_right - cdf_out_right)
        if gap_right > d_statistic:
            d_statistic = gap_right

        cdf_src_left = _binned_cdf(x, src_edges_f, src_counts, src_total, left_limit=True)
        cdf_out_left = _binned_cdf(x, out_edges_f, out_counts, out_total, left_limit=True)
        gap_left = abs(cdf_src_left - cdf_out_left)
        if gap_left > d_statistic:
            d_statistic = gap_left

    # `gap` is always >= 0 (an abs()), so d_statistic never needs a lower
    # clamp. The upper clamp is load-bearing: mass fractions summed over
    # many bins can round to a hair above 1.0 (e.g. 1.0000000000000002),
    # which would otherwise leak past the [0, 1] contract and push
    # ks_complement negative.
    d_statistic = min(1.0, d_statistic)
    ks_complement = 1.0 - d_statistic
    return {
        "value": ks_complement,
        "d_statistic": d_statistic,
        "method": _KS_METHOD,
        "comparable": True,
    }


def _binned_cdf(
    x: float,
    edges: list[float],
    counts: list[int],
    total: int,
    *,
    left_limit: bool = False,
) -> float:
    """Empirical CDF at `x` for a histogram, mass allocated uniformly per bin.

    A zero-width bin (`hi == lo`, the constant-column single-bin fallback
    in `_numeric_stats`) is a point mass: its full mass counts once `x`
    reaches it, never spread across an interval. The within-bin ramp for a
    normal (non-degenerate) bin is continuous, so it needs no left/right
    distinction; only a point mass actually jumps.

    `left_limit=True` computes lim_{t -> x-} F(t) instead of F(x): the
    point mass at `x` (if any) is excluded rather than included. Callers
    that need the true supremum of |F - G| (the KS statistic) must check
    both, since the sup can be attained approaching a jump from below, not
    only at or after it.
    """
    cumulative = 0.0
    for i, count in enumerate(counts):
        if count == 0:
            continue
        lo, hi = edges[i], edges[i + 1]
        mass = count / total
        if hi == lo:
            included = x > lo if left_limit else x >= lo
            if included:
                cumulative += mass
            continue
        if x <= lo:
            frac = 0.0
        elif x >= hi:
            frac = 1.0
        else:
            frac = (x - lo) / (hi - lo)
        cumulative += frac * mass
    return cumulative


# ── categorical: chi-square Cramer's V on a common partition ───────────────


def chi_square_cramers_v(
    src_stats: dict[str, Any],
    out_stats: dict[str, Any],
) -> dict[str, Any]:
    """Two-sample chi-square homogeneity, normalized to Cramer's V.

    Reads only `top_values` / `other_count` (never raw values). Builds the
    common partition described in the module docstring, then returns a
    dict with `value` (`chi_similarity = 1 - Cramers_V`, `None` if
    incomparable), `cramers_v`, `chi2_statistic` (both present iff
    comparable), `method`, `comparable`, and `reason` (present iff not
    comparable).
    """
    src_items = src_stats.get("top_values") or []
    out_items = out_stats.get("top_values") or []
    src_other = int(src_stats.get("other_count", 0) or 0)
    out_other = int(out_stats.get("other_count", 0) or 0)

    src_counts = {str(item["value"]): int(item["count"]) for item in src_items}
    out_counts = {str(item["value"]): int(item["count"]) for item in out_items}

    src_total = sum(src_counts.values()) + src_other
    out_total = sum(out_counts.values()) + out_other
    if src_total <= 0 or out_total <= 0:
        return _incomparable(_CHI_METHOD, "zero_mass")

    # Sorted for a fixed reduction order (byte-stability), and so the
    # partition assembly below is independent of snapshot top-K ordering.
    common_labels = sorted(set(src_counts) & set(out_counts))
    common_set = set(common_labels)

    src_other_adj = src_other + sum(
        count for label, count in src_counts.items() if label not in common_set
    )
    out_other_adj = out_other + sum(
        count for label, count in out_counts.items() if label not in common_set
    )

    # Each column is (src_count, out_count) for one shared category.
    # Common labels first (sorted), the folded "other" bucket last -- a
    # label in one side's top-K but the other's other_count remainder
    # never becomes a fabricated zero cell; it is only ever compared
    # inside this shared bucket.
    columns: list[tuple[int, int]] = [
        (src_counts[label], out_counts[label]) for label in common_labels
    ]
    if src_other_adj > 0 or out_other_adj > 0:
        columns.append((src_other_adj, out_other_adj))

    # Codex FINAL gate MEDIUM finding: a common label present as a dict
    # key on both sides but with count 0 on BOTH (e.g. a malformed/edge-
    # case top_values entry) used to count toward "identifiable support"
    # just for existing, even though it carries zero mass and contributes
    # nothing to the chi-square sum below -- inflating len(columns) past
    # the guard on what is really a single live bucket. Drop dead columns
    # FIRST, then gate on what is actually left to compare.
    live_columns = [(s, o) for s, o in columns if s > 0 or o > 0]

    if len(live_columns) < 2:
        return _incomparable(_CHI_METHOD, "insufficient_identifiable_support")

    grand_total = src_total + out_total
    chi2_statistic = 0.0
    for src_count, out_count in live_columns:
        col_total = src_count + out_count
        # col_total > 0 always here (live_columns filtered above), and
        # src_total/out_total > 0 (guarded above), so expected_* > 0 --
        # no divide-by-zero.
        expected_src = src_total * col_total / grand_total
        expected_out = out_total * col_total / grand_total
        chi2_statistic += (src_count - expected_src) ** 2 / expected_src
        chi2_statistic += (out_count - expected_out) ** 2 / expected_out

    # 2 rows (source, output) always -> min(rows - 1, cols - 1) == 1
    # whenever cols >= 2 (guaranteed by the len(live_columns) < 2 guard
    # above).
    # sqrt() is never negative, so cramers_v never needs a lower clamp; the
    # upper clamp is load-bearing for the same summed-rounding-error reason
    # as ks_complement_binned's d_statistic clamp.
    cramers_v = min(1.0, math.sqrt(chi2_statistic / grand_total))
    chi_similarity = 1.0 - cramers_v
    return {
        "value": chi_similarity,
        "cramers_v": cramers_v,
        "chi2_statistic": chi2_statistic,
        "method": _CHI_METHOD,
        "comparable": True,
    }

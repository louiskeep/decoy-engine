"""Tests for the A2 goodness-of-fit metrics (`quality/_distribution_gof.py`).

Plan: `docs/plans/2026-09-23-distribution-integrity-ks-chisquare.md`.
Covers both metric functions directly (they are pure `dict -> dict`, no
snapshot / pandas machinery needed to exercise them). Fidelity-level
wiring (extra_metrics placement, additivity, grade/overall_score
unchanged) lives in `test_fidelity.py`.
"""

from __future__ import annotations

import json
import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.quality._distribution_gof import (
    _binned_cdf,
    chi_square_cramers_v,
    ks_complement_binned,
)

# ── numeric: KS-complement ──────────────────────────────────────────────────


def _numeric(bin_edges: list[float], bin_counts: list[int]) -> dict[str, object]:
    return {"bin_edges": bin_edges, "bin_counts": bin_counts}


def test_ks_complement_identical_is_one() -> None:
    stats = _numeric([0, 10, 20, 30], [10, 20, 10])
    result = ks_complement_binned(stats, stats)
    assert result["comparable"] is True
    assert result["value"] == pytest.approx(1.0)
    assert result["d_statistic"] == pytest.approx(0.0)
    assert result["method"] == "ks_complement_binned_cdf"


def test_ks_complement_fully_shifted_near_zero() -> None:
    src = _numeric([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100], [10] * 10)
    out = _numeric([200, 210, 220, 230, 240, 250, 260, 270, 280, 290, 300], [10] * 10)
    result = ks_complement_binned(src, out)
    # Disjoint ranges: at x = 100 (top of src's range) CDF_src = 1.0 while
    # CDF_out = 0.0 (below its own bottom edge) -> D = 1.0 -> ks = 0.0.
    assert result["comparable"] is True
    assert result["d_statistic"] == pytest.approx(1.0)
    assert result["value"] == pytest.approx(0.0, abs=1e-9)


def test_ks_complement_hand_calculated_unequal_sample_sizes() -> None:
    # Same bin edges [0, 10, 20] on both sides, but different totals and
    # different within-range split -- unequal sample sizes (100 vs 200).
    # src cumulative mass at x=10: 30/100 = 0.3.
    # out cumulative mass at x=10: 180/200 = 0.9.
    # Both sides are 0 at x=0 and 1 at x=20, so the max gap is at x=10:
    # D = |0.3 - 0.9| = 0.6 -> ks_complement = 0.4.
    src = _numeric([0, 10, 20], [30, 70])
    out = _numeric([0, 10, 20], [180, 20])
    result = ks_complement_binned(src, out)
    assert result["d_statistic"] == pytest.approx(0.6)
    assert result["value"] == pytest.approx(0.4)


def test_ks_complement_identical_constants_score_1_0() -> None:
    # A constant column's histogram is the single-bin zero-width fallback
    # `_numeric_stats` emits for `lo == hi`: bin_edges = [c, c].
    src = _numeric([5.0, 5.0], [100])
    out = _numeric([5.0, 5.0], [100])
    result = ks_complement_binned(src, out)
    assert result["comparable"] is True
    assert result["value"] == pytest.approx(1.0)
    assert result["d_statistic"] == pytest.approx(0.0)


def test_ks_complement_different_constants_score_0() -> None:
    src = _numeric([5.0, 5.0], [100])
    out = _numeric([10.0, 10.0], [100])
    result = ks_complement_binned(src, out)
    assert result["comparable"] is True
    assert result["value"] == pytest.approx(0.0)
    assert result["d_statistic"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "src_stats,out_stats,reason",
    [
        (_numeric([], []), _numeric([0, 1], [1]), "missing_histogram"),
        (_numeric([0, 1], [1]), _numeric([0], []), "missing_histogram"),
        # Non-empty edges but an empty counts list on one side -- exercises
        # the "not src_counts" disjunct specifically (distinct from the
        # "len(edges) < 2" disjunct the first two cases above trip).
        (_numeric([0, 1, 2], []), _numeric([0, 1, 2], [1, 1]), "missing_histogram"),
        # Exactly one side has a short edge list while both counts lists
        # are non-empty -- exercises the "len(src_edges) < 2" disjunct on
        # its own, independent of the counts disjuncts.
        (_numeric([0], [1]), _numeric([0, 1, 2], [1, 1]), "missing_histogram"),
        (_numeric([0, 1, 2], [1]), _numeric([0, 1], [1]), "malformed_histogram"),
        (_numeric([0, 1], [0]), _numeric([0, 1], [1]), "zero_mass"),
        # Reverse of the zero_mass case above: the OUTPUT side (not the
        # source side) is the one with zero mass.
        (_numeric([0, 1], [5]), _numeric([0, 1], [0]), "zero_mass"),
    ],
)
def test_ks_complement_undefined_support_is_comparable_false(
    src_stats: dict[str, object],
    out_stats: dict[str, object],
    reason: str,
) -> None:
    # Malformed / empty / zero-mass histograms give an honest skip, never
    # a NaN or an exception.
    result = ks_complement_binned(src_stats, out_stats)
    assert result["comparable"] is False
    assert result["value"] is None
    assert result["reason"] == reason
    assert result["method"] == "ks_complement_binned_cdf"
    assert json.dumps(result)  # no NaN leaks into the JSON


def test_ks_complement_total_of_one_is_comparable() -> None:
    # A single-observation histogram (total == 1, not 0) is real, usable
    # mass -- the zero_mass guard must not fire until the total is
    # actually <= 0.
    src = _numeric([0, 1], [1])
    out = _numeric([0, 1], [1])
    result = ks_complement_binned(src, out)
    assert result["comparable"] is True
    assert result["value"] == pytest.approx(1.0)


def test_ks_complement_overshoot_clamps_to_exactly_one() -> None:
    # A many-bin histogram whose per-bin mass fractions (count / total)
    # sum to a hair above 1.0 in float64 (hand-verified: 1.0000000000000002
    # at the top edge before clamping). Compared against a disjoint
    # single-point histogram, D would leak past 1.0 without the clamp.
    edges = [
        0, 2, 3, 5, 6, 7, 9, 10, 13, 16, 18, 19, 22, 25, 26, 29, 30, 32,
        34, 36, 39, 42, 43, 45, 47, 48, 49, 51, 52, 55, 58,
    ]  # fmt: skip
    counts = [
        276, 110, 138, 389, 171, 308, 260, 431, 131, 189, 174, 175, 59,
        150, 121, 445, 484, 310, 400, 489, 367, 455, 251, 70, 297, 283,
        395, 54, 165, 21,
    ]  # fmt: skip
    src = _numeric(edges, counts)
    out = _numeric([1000, 1001], [1])
    result = ks_complement_binned(src, out)
    assert result["d_statistic"] == 1.0
    assert result["value"] == 0.0


def test_binned_cdf_below_all_bins_is_zero() -> None:
    assert _binned_cdf(-5.0, [0.0, 10.0], [5], 5) == 0.0


def test_binned_cdf_zero_count_bin_is_skipped_not_the_boundary() -> None:
    # A count-1 bin at its top edge must contribute its full mass -- this
    # pins the `count == 0` skip check to exactly 0, not 1.
    assert _binned_cdf(10.0, [0.0, 10.0], [1], 1) == 1.0


def test_binned_cdf_zero_count_bin_does_not_halt_later_bins() -> None:
    # First bin has count 0 (skipped); the loop must still reach and sum
    # the second bin's contribution rather than stopping at the skip.
    edges = [0.0, 10.0, 20.0, 30.0]
    counts = [0, 5, 5]
    assert _binned_cdf(30.0, edges, counts, 10) == pytest.approx(1.0)


def test_binned_cdf_degenerate_point_mass_boundary() -> None:
    # A zero-width bin (hi == lo) is a point mass: included once x reaches
    # it, excluded strictly before it.
    assert _binned_cdf(5.0, [5.0, 5.0], [100], 100) == 1.0
    assert _binned_cdf(4.999, [5.0, 5.0], [100], 100) == 0.0


def test_binned_cdf_degenerate_bin_accumulates_and_continues() -> None:
    # Normal bin (0, 10), then a degenerate point bin at 10, then another
    # normal bin (10, 20). At x=20 all three must have contributed --
    # this pins the degenerate branch to `cumulative += mass` (not an
    # overwrite) and to `continue` (not `break`, which would strand the
    # third bin unprocessed).
    edges = [0.0, 10.0, 10.0, 20.0]
    counts = [2, 1, 1]  # masses: 0.5, 0.25, 0.25 (total 4)
    assert _binned_cdf(20.0, edges, counts, 4) == pytest.approx(1.0)


def test_binned_cdf_interior_point_uses_linear_interpolation() -> None:
    # lo=2, hi=10 (asymmetric around 0, so +/- operator mutants on the
    # frac formula are all distinguishable from the correct 0.5).
    assert _binned_cdf(6.0, [2.0, 10.0], [1], 1) == pytest.approx(0.5)


def test_ks_complement_symmetric() -> None:
    src = _numeric([0, 5, 10, 15, 20], [5, 15, 25, 5])
    out = _numeric([0, 5, 10, 15, 20], [20, 10, 5, 15])
    forward = ks_complement_binned(src, out)
    backward = ks_complement_binned(out, src)
    assert forward["value"] == pytest.approx(backward["value"])
    assert forward["d_statistic"] == pytest.approx(backward["d_statistic"])


# ── categorical: chi-square Cramer's V ──────────────────────────────────────


def _categorical(top: list[tuple[str, int]], other_count: int = 0) -> dict[str, object]:
    return {
        "top_values": [{"value": v, "count": c} for v, c in top],
        "other_count": other_count,
    }


def test_chi_similarity_identical_is_one() -> None:
    stats = _categorical([("CA", 50), ("NY", 30), ("TX", 20)])
    result = chi_square_cramers_v(stats, stats)
    assert result["comparable"] is True
    assert result["value"] == pytest.approx(1.0)
    assert result["cramers_v"] == pytest.approx(0.0)
    assert result["chi2_statistic"] == pytest.approx(0.0)
    assert result["method"] == "chi_square_common_partition_cramers_v"


def test_chi_similarity_disjoint_categories_near_zero() -> None:
    # A single shared anchor label ("A") at wildly different proportions
    # (98% of src vs. 2% of out), with the remaining mass on value sets
    # that are otherwise fully disjoint (B on src's side; C/D on out's).
    src = _categorical([("A", 980), ("B", 20)])
    out = _categorical([("A", 20), ("C", 960), ("D", 20)])
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is True
    assert result["value"] == pytest.approx(0.04, abs=0.01)


def test_chi_similarity_hand_calculated_unequal_sizes_mismatched_topk() -> None:
    # src top-K = {A, B, X}, out top-K = {A, B, Y}: common = {A, B}: X and
    # Y are each unmatched and fold into their own side's `other`.
    #   src: A=40, B=20, other = 5 (own) + 10 (X) = 15; total = 75.
    #   out: A=30, B=15, other = 5 (own) + 20 (Y) = 25; total = 70.
    # Shared partition columns: A=(40,30), B=(20,15), other=(15,25).
    # grand_total = 145; expected_A_src = 75*70/145 = 36.2069...
    # chi2 = sum (obs-exp)^2/exp over all 6 cells = 4.475765306122452
    # (hand-verified against the two-sample chi-square homogeneity
    # formula). V = sqrt(chi2 / grand_total) with 2 rows and 3 columns
    # (min(rows-1, cols-1) = 1).
    src = _categorical([("A", 40), ("B", 20), ("X", 10)], other_count=5)
    out = _categorical([("A", 30), ("B", 15), ("Y", 20)], other_count=5)
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is True
    assert result["chi2_statistic"] == pytest.approx(4.475765306122452)
    assert result["cramers_v"] == pytest.approx(math.sqrt(4.475765306122452 / 145))
    assert result["value"] == pytest.approx(1.0 - math.sqrt(4.475765306122452 / 145))


def test_chi_similarity_label_in_one_topk_other_othercount_not_zeroed() -> None:
    # "X" sits in src's top-K (count 60) but not out's; out's other_count
    # (60) may already include X's mass at that cutoff. The common-
    # partition guard must fold X's src count into src's own `other`
    # bucket rather than reading out's cell for X as a fabricated 0 --
    # which would make these look completely different when the shared
    # partition (A=(40,40), other=(60,60)) shows they are identical.
    src = _categorical([("A", 40), ("X", 60)])
    out = _categorical([("A", 40)], other_count=60)
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is True
    assert result["value"] == pytest.approx(1.0)
    assert result["chi2_statistic"] == pytest.approx(0.0)


def test_chi_similarity_undefined_support_is_comparable_false() -> None:
    # Fully disjoint top-K sets with no other_count remainder on either
    # side: the common partition collapses to a single column (nothing
    # is identifiable as shared), which cannot support a 2-sample
    # comparison.
    src = _categorical([("A", 100)])
    out = _categorical([("B", 100)])
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is False
    assert result["value"] is None
    assert result["reason"] == "insufficient_identifiable_support"
    assert result["method"] == "chi_square_common_partition_cramers_v"
    assert json.dumps(result)


def test_chi_similarity_single_fully_covered_label_is_insufficient_support() -> None:
    # One common label, no other_count on either side, and nothing else in
    # either top-K: `other` never gets appended (both adjusted buckets are
    # exactly 0), so the shared partition is a single column -- no 2-sample
    # comparison is possible from one number vs. one number.
    src = _categorical([("A", 50)])
    out = _categorical([("A", 30)])
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is False
    assert result["reason"] == "insufficient_identifiable_support"


def test_chi_similarity_skips_zero_total_common_label_column() -> None:
    # "A" has count 0 on both sides (col_total == 0), positioned before a
    # real divergent common label "B" and the folded `other` bucket. The
    # chi-square sum must skip A (divide-by-zero guard) without halting
    # the loop, so B and `other` -- which carry the actual signal -- are
    # still summed.
    #   src: A=0, B=70, other=10; total=80.
    #   out: A=0, B=20, other=60; total=80.
    # Hand-verified chi2 over the B and other cells (A contributes
    # nothing): 63.492063492063494.
    src = _categorical([("A", 0), ("B", 70)], other_count=10)
    out = _categorical([("A", 0), ("B", 20)], other_count=60)
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is True
    assert result["chi2_statistic"] == pytest.approx(63.492063492063494)


def test_chi_similarity_col_total_of_one_is_not_skipped() -> None:
    # "A" has col_total == 1 (not 0): real, usable signal that the guard
    # must not treat as a zero-total column to skip.
    #   src: A=1, B=70, other=9; total=80.
    #   out: A=0, B=20, other=60; total=80.
    # Hand-verified chi2 including A's contribution: 66.47342995169083
    # (vs. 65.47342995169083 without it -- A alone contributes 1.0).
    src = _categorical([("A", 1), ("B", 70)], other_count=9)
    out = _categorical([("A", 0), ("B", 20)], other_count=60)
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is True
    assert result["chi2_statistic"] == pytest.approx(66.47342995169083)


def test_chi_similarity_other_bucket_included_when_only_one_side_has_leftover() -> None:
    # 2 common labels (A, B) fully cover out's mass (out_other_adj == 0
    # exactly) but src has 1 unmatched count folded to other (src_other_adj
    # == 1). The `other` column must still be appended -- it is not "both
    # sides must have leftover", just "either side does".
    src = _categorical([("A", 40), ("B", 30), ("X", 1)])
    out = _categorical([("A", 20), ("B", 15)])
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is True
    assert result["chi2_statistic"] == pytest.approx(0.4976525821596245)


def test_chi_similarity_other_bucket_included_when_only_out_side_has_leftover() -> None:
    # Mirror of the case above: src is fully covered by common labels,
    # out has the 1 unmatched leftover count.
    src = _categorical([("A", 20), ("B", 15)])
    out = _categorical([("A", 40), ("B", 30), ("Y", 1)])
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is True
    assert result["chi2_statistic"] == pytest.approx(0.4976525821596245)


def test_chi_similarity_missing_other_count_key_defaults_to_zero() -> None:
    # No `other_count` key at all (not even 0): the default must resolve
    # to 0, same as an explicit other_count=0, not silently manufacture a
    # phantom `other` bucket out of a wrong default.
    src = {"top_values": [{"value": "A", "count": 10}]}
    out = {"top_values": [{"value": "A", "count": 10}]}
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is False
    assert result["reason"] == "insufficient_identifiable_support"


def test_chi_similarity_total_of_one_is_not_zero_mass() -> None:
    # total == 1 (not 0): the zero_mass guard is about total <= 0, not
    # total <= 1. This single-observation-both-sides case is genuinely
    # comparable support (the "insufficient_identifiable_support" reason
    # distinguishes it from what a mis-tightened zero_mass guard would
    # report here).
    src = _categorical([("A", 1)])
    out = _categorical([("A", 1)])
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is False
    assert result["reason"] == "insufficient_identifiable_support"


def test_chi_similarity_out_side_zero_mass_is_comparable_false() -> None:
    # Reverse of test_chi_similarity_zero_mass_is_comparable_false below:
    # the OUTPUT side (not the source side) is the one with zero mass.
    src = _categorical([("A", 5)])
    out = _categorical([])
    result = chi_square_cramers_v(src, out)
    assert result["comparable"] is False
    assert result["reason"] == "zero_mass"
    assert result["method"] == "chi_square_common_partition_cramers_v"


def test_chi_similarity_zero_mass_is_comparable_false() -> None:
    result = chi_square_cramers_v(_categorical([]), _categorical([("A", 1)]))
    assert result["comparable"] is False
    assert result["value"] is None
    assert result["reason"] == "zero_mass"
    assert result["method"] == "chi_square_common_partition_cramers_v"


def test_chi_similarity_overshoot_clamps_to_exactly_one() -> None:
    # A 9-column, mostly-disjoint-per-column layout whose Cramer's V
    # (hand-verified against this exact code path) computes to
    # 1.0000000000000002 in float64 before clamping (raw chi2_statistic
    # 2412.000000000001 against grand_total 2412). Found by targeted
    # random search over the real function, not hand-derived -- summed
    # rounding error in chi-square accumulation is sensitive to the exact
    # column order and doesn't reproduce from a naively similar layout.
    src = _categorical(
        [
            ("L0", 0),
            ("L1", 450),
            ("L2", 310),
            ("L3", 196),
            ("L4", 112),
            ("L5", 365),
            ("L6", 0),
            ("L7", 0),
            ("L8", 0),
        ],
    )
    out = _categorical(
        [
            ("L0", 249),
            ("L1", 0),
            ("L2", 0),
            ("L3", 0),
            ("L4", 0),
            ("L5", 0),
            ("L6", 406),
            ("L7", 68),
            ("L8", 208),
        ],
        other_count=48,
    )
    result = chi_square_cramers_v(src, out)
    assert result["cramers_v"] == 1.0
    assert result["value"] == 0.0


def test_chi_similarity_symmetric() -> None:
    src = _categorical([("A", 40), ("B", 30), ("C", 10)], other_count=20)
    out = _categorical([("A", 10), ("B", 50), ("D", 15)], other_count=25)
    forward = chi_square_cramers_v(src, out)
    backward = chi_square_cramers_v(out, src)
    assert forward["value"] == pytest.approx(backward["value"])
    assert forward["chi2_statistic"] == pytest.approx(backward["chi2_statistic"])


# ── property tests: bounded [0, 1] over randomized snapshots ───────────────


@st.composite
def _numeric_stats_strategy(draw: st.DrawFn) -> dict[str, object]:
    n_bins = draw(st.integers(min_value=1, max_value=6))
    start = draw(st.integers(min_value=-1000, max_value=1000))
    widths = draw(
        st.lists(st.integers(min_value=0, max_value=200), min_size=n_bins, max_size=n_bins)
    )
    edges = [start]
    for w in widths:
        edges.append(edges[-1] + w)
    counts = draw(
        st.lists(st.integers(min_value=0, max_value=200), min_size=n_bins, max_size=n_bins)
    )
    return {"bin_edges": edges, "bin_counts": counts}


@st.composite
def _categorical_stats_strategy(draw: st.DrawFn) -> dict[str, object]:
    labels = draw(
        st.lists(
            st.sampled_from(list("ABCDEFGHIJ")),
            min_size=0,
            max_size=6,
            unique=True,
        )
    )
    counts = draw(
        st.lists(
            st.integers(min_value=1, max_value=200), min_size=len(labels), max_size=len(labels)
        )
    )
    other = draw(st.integers(min_value=0, max_value=200))
    return {
        "top_values": [{"value": v, "count": c} for v, c in zip(labels, counts, strict=True)],
        "other_count": other,
    }


@settings(
    max_examples=200, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much]
)
@given(src=_numeric_stats_strategy(), out=_numeric_stats_strategy())
def test_ks_complement_bounded_0_1(src: dict[str, object], out: dict[str, object]) -> None:
    result = ks_complement_binned(src, out)
    if result["comparable"]:
        assert 0.0 <= result["value"] <= 1.0
        assert 0.0 <= result["d_statistic"] <= 1.0
    else:
        assert result["value"] is None


@settings(
    max_examples=200, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much]
)
@given(src=_categorical_stats_strategy(), out=_categorical_stats_strategy())
def test_chi_similarity_bounded_0_1(src: dict[str, object], out: dict[str, object]) -> None:
    result = chi_square_cramers_v(src, out)
    if result["comparable"]:
        assert 0.0 <= result["value"] <= 1.0
        assert 0.0 <= result["cramers_v"] <= 1.0
        assert result["chi2_statistic"] >= 0.0
    else:
        assert result["value"] is None

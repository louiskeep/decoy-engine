"""D7b: holdout DCR (Distance to Closest Record) tests.

Sprint plan acceptance for DCR:
  - Holdout DCR sanity check where holdout exists.
  - Report states that DCR is not a privacy guarantee. (D7a covers
    the disclaimer; this file pins the per-call warning string.)

Beyond the acceptance criteria, these tests pin the
memorization-vs-generalization signal: a synth that copies
source rows should score `memorizing`; a synth drawn from the
same distribution as both source and holdout should score
`generalizing`.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from decoy_engine.quality.synth_report import (
    assemble_synth_report,
    compute_dcr,
)


# ── core: synth-to-source distances ───────────────────────────────────────


class TestSynthToSource:
    def test_exact_copy_has_zero_dcr(self):
        """A synth row that exactly matches a source row has DCR=0."""
        source = pd.DataFrame({
            "a": [1.0, 2.0, 3.0, 4.0, 5.0],
            "b": ["x", "y", "z", "p", "q"],
        })
        synth = source.copy()
        result = compute_dcr(source, synth)
        assert result["synth_to_source"]["median"] == 0.0
        assert result["synth_to_source"]["p95"] == 0.0

    def test_distant_synth_has_higher_dcr(self):
        """A synth far from any source row has DCR ~= 1 per col."""
        source = pd.DataFrame({"x": [0.0, 1.0, 2.0]})
        # Synth values are far past the source range.
        synth = pd.DataFrame({"x": [100.0, 200.0, 300.0]})
        result = compute_dcr(source, synth)
        # Clipped to 1.0 per column (range = 2, synth = 100 -> 100/2
        # = 50, clipped to 1.0). Mean across 1 column -> 1.0.
        assert result["synth_to_source"]["median"] == 1.0

    def test_returns_only_aggregate_stats(self):
        """Per security req: no per-row distances in the result."""
        rng = np.random.default_rng(0)
        source = pd.DataFrame({"v": rng.normal(0, 1, 100)})
        synth = pd.DataFrame({"v": rng.normal(0, 1, 50)})
        result = compute_dcr(source, synth)
        # Only aggregate keys; no list-of-floats.
        block = result["synth_to_source"]
        assert set(block.keys()) >= {
            "median", "p05", "p25", "p75", "p95",
            "rows_sampled", "source_rows_sampled",
        }
        # The dict contains no per-row arrays anywhere.
        for value in block.values():
            assert not isinstance(value, (list, np.ndarray))


# ── core: holdout comparison (memorization vs generalization) ─────────────


class TestHoldoutComparison:
    def test_memorizing_when_synth_copies_source(self):
        """Synth that copies source should score 'memorizing' against
        a holdout drawn from the same distribution."""
        rng = np.random.default_rng(42)
        source = pd.DataFrame({"v": rng.normal(0, 1, 200)})
        # Synth = exact copy of source -> DCR_source ~= 0
        synth = source.copy()
        # Holdout = independent draw from same distribution
        holdout = pd.DataFrame({"v": rng.normal(0, 1, 200)})

        result = compute_dcr(source, synth, holdout=holdout)
        comp = result["comparison"]
        assert comp["interpretation"] == "memorizing"
        # median(source)=0, median(holdout)>0 -> ratio < 0.5
        assert comp["median_ratio"] is not None
        assert comp["median_ratio"] < 0.5

    def test_generalizing_when_synth_is_independent(self):
        """Synth from the same distribution as source AND holdout
        should score 'generalizing': both medians similar."""
        rng = np.random.default_rng(13)
        source = pd.DataFrame({"v": rng.normal(0, 1, 500)})
        synth = pd.DataFrame({"v": rng.normal(0, 1, 500)})
        holdout = pd.DataFrame({"v": rng.normal(0, 1, 500)})

        result = compute_dcr(source, synth, holdout=holdout)
        # The two medians should be close; ratio in the
        # generalizing band.
        assert result["comparison"]["interpretation"] == "generalizing"

    def test_no_holdout_leaves_comparison_empty(self):
        source = pd.DataFrame({"v": [1, 2, 3, 4, 5]})
        synth = pd.DataFrame({"v": [1, 2, 3, 4, 5]})
        result = compute_dcr(source, synth)
        assert result["synth_to_holdout"] is None
        assert result["comparison"]["interpretation"] is None
        assert result["comparison"]["median_ratio"] is None

    def test_empty_holdout_leaves_comparison_empty(self):
        source = pd.DataFrame({"v": [1, 2, 3]})
        synth = pd.DataFrame({"v": [1, 2, 3]})
        holdout = pd.DataFrame({"v": []})
        result = compute_dcr(source, synth, holdout=holdout)
        assert result["synth_to_holdout"] is None


# ── Gower mixed-type distance ─────────────────────────────────────────────


class TestGowerMixedTypes:
    def test_numeric_distance_range_normalized(self):
        """Distance is normalized by source range."""
        source = pd.DataFrame({"v": [0.0, 100.0]})  # range = 100
        # synth=50 -> halfway between 0 and 100, dist=0.5
        synth = pd.DataFrame({"v": [50.0]})
        result = compute_dcr(source, synth)
        # The closer of [0, 100] to 50 is either; both have dist=0.5
        assert result["synth_to_source"]["median"] == 0.5

    def test_categorical_distance_is_zero_or_one(self):
        """Mismatching categorical column contributes 1, matching 0."""
        source = pd.DataFrame({"c": ["A", "B", "C"]})
        synth = pd.DataFrame({"c": ["A", "Z"]})  # A matches, Z doesn't
        result = compute_dcr(source, synth)
        # min distance for "A" row = 0, for "Z" row = 1
        # median([0, 1]) = 0.5
        assert result["synth_to_source"]["median"] == 0.5

    def test_mixed_numeric_and_categorical(self):
        """Two-column frame: per-row distance = mean of per-column
        distances."""
        source = pd.DataFrame({
            "n": [0.0, 1.0],
            "c": ["A", "B"],
        })
        # Synth row: n=0 (matches first source row exactly), c="X"
        # (mismatch on both). So distance to source[0] = (0 + 1)/2 = 0.5
        # distance to source[1] = (1 + 1)/2 = 1.0. min = 0.5.
        synth = pd.DataFrame({"n": [0.0], "c": ["X"]})
        result = compute_dcr(source, synth)
        assert result["synth_to_source"]["median"] == 0.5

    def test_numeric_nan_treated_as_max_distance(self):
        """NaN in either side -> per-column distance is 1.0."""
        source = pd.DataFrame({"v": [1.0, 2.0, 3.0]})
        synth = pd.DataFrame({"v": [float("nan")]})
        result = compute_dcr(source, synth)
        # NaN -> all distances are 1.0, min = 1.0
        assert result["synth_to_source"]["median"] == 1.0

    def test_numeric_out_of_range_clipped_to_one(self):
        """A synth far past source range can't dominate other columns."""
        source = pd.DataFrame({
            "n": [0.0, 1.0, 2.0],   # range = 2
            "c": ["A", "B", "C"],
        })
        # n=1000 -> raw distance = 1000/2 = 500, clipped to 1.
        # c="C" -> matches source[2] (distance 0).
        # row distance = (1 + 0) / 2 = 0.5
        synth = pd.DataFrame({"n": [1000.0], "c": ["C"]})
        result = compute_dcr(source, synth)
        assert result["synth_to_source"]["median"] == 0.5


# ── controls ──────────────────────────────────────────────────────────────


class TestSubsetAndCap:
    def test_subset_columns_narrows_comparison(self):
        source = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        synth = pd.DataFrame({"a": [1, 2, 3], "b": ["X", "Y", "Z"]})
        # All a matches, no b matches.
        on_b = compute_dcr(source, synth, subset_columns=["b"])
        assert on_b["synth_to_source"]["median"] == 1.0
        on_a = compute_dcr(source, synth, subset_columns=["a"])
        assert on_a["synth_to_source"]["median"] == 0.0

    def test_sample_cap_truncates_deterministically(self):
        """sample_cap uses head() so the same call always returns
        the same DCR. Two calls with the same cap must agree."""
        source = pd.DataFrame({"v": list(range(10000))})
        synth = pd.DataFrame({"v": list(range(5000, 6000))})
        r1 = compute_dcr(source, synth, sample_cap=500)
        r2 = compute_dcr(source, synth, sample_cap=500)
        assert r1 == r2
        assert r1["synth_to_source"]["rows_sampled"] == 500
        assert r1["synth_to_source"]["source_rows_sampled"] == 500


# ── edge / unavailable ────────────────────────────────────────────────────


class TestUnavailable:
    def test_none_inputs(self):
        r = compute_dcr(None, None)
        assert r["synth_to_source"] is None
        assert "not provided" in r["warning"]

    def test_empty_source(self):
        r = compute_dcr(pd.DataFrame({"v": []}), pd.DataFrame({"v": [1]}))
        assert r["synth_to_source"] is None
        assert "empty" in r["warning"]

    def test_no_overlap_returns_unavailable(self):
        source = pd.DataFrame({"x": [1, 2, 3]})
        synth = pd.DataFrame({"y": [4, 5, 6]})
        r = compute_dcr(source, synth)
        assert r["synth_to_source"] is None
        assert "no overlapping columns" in r["warning"]


# ── warning + report wiring ───────────────────────────────────────────────


class TestPerCallWarning:
    def test_warning_says_not_a_privacy_guarantee(self):
        """Every successful DCR call carries the explicit disclaimer."""
        source = pd.DataFrame({"v": [1, 2, 3]})
        synth = pd.DataFrame({"v": [4, 5, 6]})
        r = compute_dcr(source, synth)
        assert r["warning"] is not None
        assert "not a privacy guarantee" in r["warning"].lower()


class TestAssembleWithDcr:
    def test_assemble_accepts_dcr_block(self):
        source = pd.DataFrame({"v": [1, 2, 3]})
        synth = pd.DataFrame({"v": [1, 2, 3]})
        dcr = compute_dcr(source, synth)
        report = assemble_synth_report(new_row_synthesis=None, dcr=dcr)
        assert report["dcr"]["synth_to_source"]["median"] == 0.0

    def test_assemble_default_dcr_none(self):
        report = assemble_synth_report(new_row_synthesis=None)
        assert report["dcr"] is None

    def test_full_report_round_trips_json(self):
        source = pd.DataFrame({"v": [1.0, 2.0, 3.0]})
        synth = pd.DataFrame({"v": [1.1, 2.0, 3.5]})
        holdout = pd.DataFrame({"v": [1.5, 2.5, 3.5]})
        dcr = compute_dcr(source, synth, holdout=holdout)
        report = assemble_synth_report(new_row_synthesis=None, dcr=dcr, job_id=1)
        encoded = json.dumps(report)
        decoded = json.loads(encoded)
        assert decoded["dcr"]["metric"] == "distance_to_closest_record"

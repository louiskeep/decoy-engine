"""D6c: distribution-driven datetime generation tests.

Mirrors D6a (numeric) + D6b (categorical). Datetime snapshot shape
per D1a `_datetime_stats`:

    stats:
      min: "2020-01-01T00:00:00"
      max: "2024-12-31T23:59:59"
      year_bins: [{year: 2020, count: N}, ...]

Sampler: weighted year choice -> uniform within year, clipped to
[min, max].
"""
from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd
import pytest

from decoy_engine.errors import ValidationError
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.graph.ops.generate_op import validate_config


class TestValidator:
    def test_datetime_distribution_accepted(self):
        validate_config({
            "columns": {
                "ts": {
                    "strategy": "distribution",
                    "snapshot": {
                        "kind": "datetime",
                        "min": "2020-01-01T00:00:00",
                        "max": "2024-12-31T00:00:00",
                        "year_bins": [{"year": 2022, "count": 10}],
                    },
                },
            },
        })


def _gen() -> ColumnGenerator:
    return ColumnGenerator(seed=42)


class TestDatetimeSampling:
    def test_all_samples_within_min_max(self):
        snap = {
            "kind": "datetime",
            "stats": {
                "min": "2020-06-01T00:00:00",
                "max": "2024-03-15T00:00:00",
                "year_bins": [
                    {"year": 2020, "count": 100},
                    {"year": 2021, "count": 200},
                    {"year": 2022, "count": 300},
                    {"year": 2023, "count": 200},
                    {"year": 2024, "count": 50},
                ],
            },
        }
        out = _gen().generate_column(
            num_rows=2000,
            column_config={
                "name": "ts", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert pd.api.types.is_datetime64_any_dtype(out)
        lo = pd.Timestamp("2020-06-01T00:00:00")
        hi = pd.Timestamp("2024-03-15T00:00:00")
        assert (out >= lo).all(), f"some samples < min ({out[out < lo].head()})"
        assert (out <= hi).all(), f"some samples > max ({out[out > hi].head()})"

    def test_year_weights_drive_year_frequency(self):
        """A year_bins distribution with one dominant year should
        produce mostly that year in the output."""
        snap = {
            "kind": "datetime",
            "stats": {
                "min": "2020-01-01T00:00:00",
                "max": "2023-12-31T00:00:00",
                "year_bins": [
                    {"year": 2020, "count": 10},
                    {"year": 2021, "count": 10},
                    {"year": 2022, "count": 900},   # dominant
                    {"year": 2023, "count": 10},
                ],
            },
        }
        out = _gen().generate_column(
            num_rows=1000,
            column_config={
                "name": "y", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        year_counts = Counter(out.dt.year.tolist())
        # 900/930 ~= 96.7%; allow 85% as the slack bar.
        assert year_counts[2022] > 850

    def test_partial_first_year_clips_to_min(self):
        """When min is 2020-06-01, no 2020 samples land in Jan-May."""
        snap = {
            "kind": "datetime",
            "stats": {
                "min": "2020-06-01T00:00:00",
                "max": "2020-12-31T00:00:00",
                "year_bins": [{"year": 2020, "count": 100}],
            },
        }
        out = _gen().generate_column(
            num_rows=500,
            column_config={
                "name": "p", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        june_first = pd.Timestamp("2020-06-01T00:00:00")
        assert (out >= june_first).all()

    def test_determinism(self):
        snap = {
            "kind": "datetime",
            "stats": {
                "min": "2022-01-01T00:00:00",
                "max": "2022-12-31T23:59:59",
                "year_bins": [{"year": 2022, "count": 100}],
            },
        }
        cfg = {"name": "d", "type": "distribution", "snapshot": snap}
        s1 = _gen().generate_column(100, cfg, "t", {})
        s2 = _gen().generate_column(100, cfg, "t", {})
        pd.testing.assert_series_equal(s1, s2)


class TestEdgeCases:
    def test_missing_year_bins_emits_nat(self):
        snap = {
            "kind": "datetime",
            "stats": {
                "min": "2020-01-01T00:00:00",
                "max": "2024-12-31T00:00:00",
                "year_bins": [],
            },
        }
        out = _gen().generate_column(
            num_rows=5,
            column_config={
                "name": "x", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert out.isna().all()

    def test_missing_min_max_emits_nat(self):
        snap = {
            "kind": "datetime",
            "stats": {"year_bins": [{"year": 2022, "count": 10}]},
        }
        out = _gen().generate_column(
            num_rows=5,
            column_config={
                "name": "y", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert out.isna().all()

    def test_unparseable_min_emits_nat(self):
        snap = {
            "kind": "datetime",
            "stats": {
                "min": "not-a-date",
                "max": "2024-01-01T00:00:00",
                "year_bins": [{"year": 2022, "count": 10}],
            },
        }
        out = _gen().generate_column(
            num_rows=5,
            column_config={
                "name": "z", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert out.isna().all()

    def test_max_before_min_emits_nat(self):
        snap = {
            "kind": "datetime",
            "stats": {
                "min": "2024-01-01T00:00:00",
                "max": "2020-01-01T00:00:00",
                "year_bins": [{"year": 2022, "count": 10}],
            },
        }
        out = _gen().generate_column(
            num_rows=5,
            column_config={
                "name": "rev", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert out.isna().all()

    def test_malformed_year_bin_entries_skipped(self):
        """Only the valid {year, count} entries should contribute."""
        snap = {
            "kind": "datetime",
            "stats": {
                "min": "2022-01-01T00:00:00",
                "max": "2022-12-31T23:59:59",
                "year_bins": [
                    "not-a-dict",
                    {"year": "huh", "count": 10},  # bad year
                    {"year": 2022, "count": "lots"},  # bad count
                    {"year": 2022, "count": 100},  # valid
                ],
            },
        }
        out = _gen().generate_column(
            num_rows=50,
            column_config={
                "name": "m", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert (out.dt.year == 2022).all()


class TestDatetimeMatchesSource:
    """Capstone: generated datetime distribution matches source via D5b."""

    def test_generated_column_matches_source_shape(self):
        from decoy_engine.quality.shape_fidelity import compute_shape_fidelity
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        rng = np.random.default_rng(777)
        # Build a multi-year source skewed toward 2022.
        years = rng.choice(
            [2020, 2021, 2022, 2023, 2024],
            size=5000,
            p=[0.10, 0.15, 0.45, 0.20, 0.10],
        )
        # Random day-of-year per row.
        days = rng.integers(0, 360, size=5000)
        source_ts = pd.to_datetime([
            f"{y}-01-01" for y in years
        ]) + pd.to_timedelta(days, unit="D")
        source = pd.DataFrame({"ts": source_ts})
        src_snap = compute_distribution_snapshot(source)
        src_ts_snap = src_snap["columns"]["ts"]
        assert src_ts_snap["kind"] == "datetime"

        synth = _gen().generate_column(
            num_rows=5000,
            column_config={
                "name": "ts", "type": "distribution", "snapshot": src_ts_snap,
            },
            table_name="t",
            reference_data={},
        )
        synth_snap = compute_distribution_snapshot(pd.DataFrame({"ts": synth}))
        shape = compute_shape_fidelity(src_snap, synth_snap)
        score = shape["marginal"]["columns"][0]["shape_similarity"]
        assert score is not None
        # Datetime shape_fidelity uses the year-bin distribution
        # directly; the within-year uniform draw doesn't affect
        # the bin counts. Tight bar.
        assert score >= 0.90, (
            f"shape_similarity {score} below 0.90 for datetime capstone"
        )

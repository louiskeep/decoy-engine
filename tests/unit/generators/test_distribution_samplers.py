"""Coverage-audit tests for the distribution-snapshot samplers.

Targets ``decoy_engine.generators._distribution._DistributionMixin`` (the
numeric / categorical / datetime samplers folded into ``ColumnGenerator``),
which the pre-existing distribution tests exercised only indirectly through
higher layers. These lock the sampler contract directly: the dispatch on
``snapshot.kind``, every degenerate / error branch that emits nulls with a
warning, and the deterministic normal paths.

Determinism note: the numeric / categorical / datetime samplers take an
explicit integer ``seed`` and drive ``np.random.default_rng(seed)``, so a
fixed seed pins the exact output. Tests assert real properties (domain,
length, dtype, weight shape, byte-identical repeats) rather than golden
literals, except where the sampler is fully deterministic under the seed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.generators.columns import ColumnGenerator

SEED = 20260728


@pytest.fixture
def cg() -> ColumnGenerator:
    return ColumnGenerator(seed=42)


# --------------------------------------------------------------------------
# Dispatcher: _generate_distribution_column
# --------------------------------------------------------------------------
class TestDispatch:
    def test_missing_snapshot_dict_emits_nulls(self, cg):
        out = cg._generate_distribution_column(7, {"name": "x"}, "t", {})
        assert len(out) == 7
        assert out.isna().all()

    def test_non_dict_snapshot_emits_nulls(self, cg):
        out = cg._generate_distribution_column(
            5, {"name": "x", "snapshot": ["not", "a", "dict"]}, "t", {}
        )
        assert len(out) == 5
        assert out.isna().all()

    def test_unsupported_kind_emits_nulls(self, cg):
        out = cg._generate_distribution_column(
            4, {"name": "x", "snapshot": {"kind": "freetext"}}, "t", {}
        )
        assert len(out) == 4
        assert out.isna().all()

    def test_missing_kind_emits_nulls(self, cg):
        # kind absent -> "" -> unsupported branch.
        out = cg._generate_distribution_column(
            3, {"name": "x", "snapshot": {"bin_edges": [0, 1], "bin_counts": [5]}}, "t", {}
        )
        assert len(out) == 3
        assert out.isna().all()

    def test_dispatch_numeric(self, cg):
        snap = {"kind": "numeric", "bin_edges": [0.0, 10.0], "bin_counts": [5]}
        out = cg._generate_distribution_column(20, {"name": "n", "snapshot": snap}, "t", {})
        assert len(out) == 20
        assert out.between(0.0, 10.0).all()

    def test_dispatch_categorical(self, cg):
        snap = {"kind": "categorical", "top_values": [{"value": "A", "count": 1}]}
        out = cg._generate_distribution_column(6, {"name": "c", "snapshot": snap}, "t", {})
        assert set(out) == {"A"}

    def test_dispatch_datetime(self, cg):
        snap = {
            "kind": "datetime",
            "min": "2020-01-01T00:00:00",
            "max": "2021-12-31T00:00:00",
            "year_bins": [{"year": 2020, "count": 1}, {"year": 2021, "count": 1}],
        }
        out = cg._generate_distribution_column(8, {"name": "d", "snapshot": snap}, "t", {})
        assert str(out.dtype).startswith("datetime64")

    def test_kind_dispatch_is_case_insensitive(self, cg):
        snap = {"kind": "NUMERIC", "bin_edges": [0.0, 1.0], "bin_counts": [5]}
        out = cg._generate_distribution_column(5, {"name": "n", "snapshot": snap}, "t", {})
        assert out.between(0.0, 1.0).all()


# --------------------------------------------------------------------------
# Numeric sampler: _generate_distribution_numeric
# --------------------------------------------------------------------------
class TestNumeric:
    def test_missing_bins_emits_nulls(self, cg):
        assert cg._generate_distribution_numeric(5, {}, SEED).isna().all()
        assert cg._generate_distribution_numeric(5, {"bin_edges": [0, 1]}, SEED).isna().all()
        assert cg._generate_distribution_numeric(5, {"bin_counts": [5]}, SEED).isna().all()

    def test_shape_mismatch_emits_nulls(self, cg):
        # edges must be counts + 1; here edges == counts -> mismatch.
        snap = {"bin_edges": [0.0, 10.0], "bin_counts": [5, 5]}
        out = cg._generate_distribution_numeric(6, snap, SEED)
        assert len(out) == 6
        assert out.isna().all()

    def test_zero_total_count_emits_nulls(self, cg):
        snap = {"bin_edges": [0.0, 10.0, 20.0], "bin_counts": [0, 0]}
        out = cg._generate_distribution_numeric(6, snap, SEED)
        assert out.isna().all()

    def test_constant_column_single_zero_range_bin(self, cg):
        snap = {"bin_edges": [42.5, 42.5], "bin_counts": [100]}
        out = cg._generate_distribution_numeric(9, snap, SEED)
        assert out.tolist() == [42.5] * 9

    def test_normal_path_range_length_and_float_dtype(self, cg):
        snap = {"kind": "numeric", "bin_edges": [0.0, 10.0, 20.0], "bin_counts": [3, 1]}
        out = cg._generate_distribution_numeric(1000, snap, SEED)
        assert len(out) == 1000
        assert out.between(0.0, 20.0).all()
        assert all(isinstance(v, float) for v in out)

    def test_weighted_bins_respect_probabilities(self, cg):
        # 3:1 weight on the [0,10) bin vs [10,20) bin -> ~75% land in the low bin.
        snap = {"bin_edges": [0.0, 10.0, 20.0], "bin_counts": [3, 1]}
        out = cg._generate_distribution_numeric(4000, snap, SEED)
        low_frac = (out < 10.0).mean()
        assert 0.70 <= low_frac <= 0.80

    def test_deterministic_under_same_seed(self, cg):
        snap = {"bin_edges": [0.0, 5.0, 10.0], "bin_counts": [2, 3]}
        a = cg._generate_distribution_numeric(200, snap, SEED)
        b = cg._generate_distribution_numeric(200, snap, SEED)
        assert a.tolist() == b.tolist()

    def test_different_seed_diverges(self, cg):
        snap = {"bin_edges": [0.0, 5.0, 10.0], "bin_counts": [2, 3]}
        a = cg._generate_distribution_numeric(200, snap, SEED)
        b = cg._generate_distribution_numeric(200, snap, SEED + 1)
        assert a.tolist() != b.tolist()

    def test_reads_nested_stats_block(self, cg):
        # Real snapshots nest the histogram under `stats`.
        snap = {"kind": "numeric", "stats": {"bin_edges": [0.0, 4.0], "bin_counts": [7]}}
        out = cg._generate_distribution_numeric(50, snap, SEED)
        assert out.between(0.0, 4.0).all()


# --------------------------------------------------------------------------
# Categorical sampler: _generate_distribution_categorical
# --------------------------------------------------------------------------
class TestCategorical:
    def test_empty_snapshot_emits_nulls(self, cg):
        out = cg._generate_distribution_categorical(5, {}, SEED)
        assert len(out) == 5
        assert out.isna().all()

    def test_all_zero_weight_entries_emit_nulls(self, cg):
        snap = {"top_values": [{"value": "A", "count": 0}, {"value": "B", "count": 0}]}
        out = cg._generate_distribution_categorical(5, snap, SEED)
        assert out.isna().all()

    def test_malformed_entries_are_skipped(self, cg):
        snap = {
            "top_values": [
                "not-a-dict",
                {"value": None, "count": 5},
                {"value": "X", "count": "not-numeric"},
                {"value": "Y", "count": 0},
                {"value": "Z", "count": 10},
            ]
        }
        out = cg._generate_distribution_categorical(100, snap, SEED)
        assert set(out) == {"Z"}

    def test_values_are_a_subset_of_declared_categories(self, cg):
        snap = {"top_values": [{"value": "A", "count": 80}, {"value": "B", "count": 20}]}
        out = cg._generate_distribution_categorical(1000, snap, SEED)
        assert set(out) <= {"A", "B"}
        assert out.dtype == object

    def test_majority_class_frequency_shape(self, cg):
        snap = {"top_values": [{"value": "A", "count": 80}, {"value": "B", "count": 20}]}
        out = cg._generate_distribution_categorical(4000, snap, SEED)
        assert 0.75 <= (out == "A").mean() <= 0.85

    def test_other_tail_bucket_default_label(self, cg):
        snap = {"top_values": [{"value": "A", "count": 50}], "other_count": 50}
        out = cg._generate_distribution_categorical(1000, snap, SEED)
        assert set(out) == {"A", "<other>"}

    def test_other_label_override(self, cg):
        snap = {
            "top_values": [{"value": "A", "count": 50}],
            "other_count": 50,
            "other_label": "MASKED",
        }
        out = cg._generate_distribution_categorical(1000, snap, SEED)
        assert "MASKED" in set(out)
        assert set(out) <= {"A", "MASKED"}

    def test_non_numeric_other_count_ignored(self, cg):
        # other_count that won't float should collapse to no tail bucket.
        snap = {"top_values": [{"value": "A", "count": 10}], "other_count": "lots"}
        out = cg._generate_distribution_categorical(20, snap, SEED)
        assert set(out) == {"A"}

    def test_deterministic_under_same_seed(self, cg):
        snap = {"top_values": [{"value": "A", "count": 3}, {"value": "B", "count": 1}]}
        a = cg._generate_distribution_categorical(300, snap, SEED)
        b = cg._generate_distribution_categorical(300, snap, SEED)
        assert a.tolist() == b.tolist()

    def test_reads_nested_stats_block(self, cg):
        snap = {"stats": {"top_values": [{"value": "Q", "count": 5}], "other_count": 0}}
        out = cg._generate_distribution_categorical(30, snap, SEED)
        assert set(out) == {"Q"}


# --------------------------------------------------------------------------
# Datetime sampler: _generate_distribution_datetime
# --------------------------------------------------------------------------
class TestDatetime:
    def _snap(self, **over):
        base = {
            "min": "2020-06-01T00:00:00",
            "max": "2024-03-15T00:00:00",
            "year_bins": [{"year": 2021, "count": 100}, {"year": 2022, "count": 100}],
        }
        base.update(over)
        return base

    def test_missing_year_bins_emits_nat(self, cg):
        snap = {"min": "2020-01-01", "max": "2021-01-01"}
        out = cg._generate_distribution_datetime(5, snap, SEED)
        assert len(out) == 5
        assert out.isna().all()
        assert str(out.dtype).startswith("datetime64")

    def test_missing_min_or_max_emits_nat(self, cg):
        snap = {"year_bins": [{"year": 2021, "count": 1}], "min": "2020-01-01"}
        out = cg._generate_distribution_datetime(5, snap, SEED)
        assert out.isna().all()

    def test_unparseable_min_max_emits_nat(self, cg):
        snap = self._snap(min="not-a-date", max="also-bad")
        out = cg._generate_distribution_datetime(5, snap, SEED)
        assert out.isna().all()

    def test_max_before_min_emits_nat(self, cg):
        snap = self._snap(min="2024-01-01T00:00:00", max="2020-01-01T00:00:00")
        out = cg._generate_distribution_datetime(5, snap, SEED)
        assert out.isna().all()

    def test_malformed_year_entries_skipped(self, cg):
        snap = self._snap(
            year_bins=[
                "not-a-dict",
                {"year": "bad", "count": 5},
                {"year": 2022, "count": 0},
                {"year": 2023, "count": 10},
            ]
        )
        out = cg._generate_distribution_datetime(50, snap, SEED)
        assert out.notna().all()
        # Every draw resolves to the only usable year (2023), clipped to bounds.
        assert (out.dt.year == 2023).all()

    def test_no_usable_years_emits_nat(self, cg):
        snap = self._snap(year_bins=[{"year": 2021, "count": 0}, {"year": 2022, "count": 0}])
        out = cg._generate_distribution_datetime(5, snap, SEED)
        assert out.isna().all()

    def test_normal_path_range_dtype_and_length(self, cg):
        snap = self._snap()
        out = cg._generate_distribution_datetime(1000, snap, SEED)
        assert len(out) == 1000
        assert str(out.dtype).startswith("datetime64")
        assert (out >= pd.Timestamp("2020-06-01")).all()
        assert (out <= pd.Timestamp("2024-03-15")).all()

    def test_deterministic_under_same_seed(self, cg):
        snap = self._snap()
        a = cg._generate_distribution_datetime(200, snap, SEED)
        b = cg._generate_distribution_datetime(200, snap, SEED)
        assert a.tolist() == b.tolist()

    def test_tz_aware_bounds_are_normalized(self, cg):
        # tz-aware min/max must not raise; output stays tz-naive datetime64.
        snap = self._snap(min="2021-01-01T00:00:00+05:00", max="2022-12-31T00:00:00+05:00")
        out = cg._generate_distribution_datetime(50, snap, SEED)
        assert out.notna().all()
        assert out.dt.tz is None

    def test_year_beyond_max_is_capped_into_bounds(self, cg):
        # A year_bin far past max must not blow the [min, max] envelope
        # (F7 OutOfBoundsDatetime guard): rows land at ts_max.year.
        snap = self._snap(year_bins=[{"year": 2200, "count": 100}])
        out = cg._generate_distribution_datetime(50, snap, SEED)
        assert out.notna().all()
        assert (out <= pd.Timestamp("2024-03-15")).all()
        assert (out.dt.year == 2024).all()

    def test_year_below_min_clamps_to_min(self, cg):
        # A year entirely below min inverts the clipped window; the defensive
        # clamp emits ts_min rather than nulls or an out-of-range draw.
        snap = self._snap(year_bins=[{"year": 2010, "count": 100}])
        out = cg._generate_distribution_datetime(20, snap, SEED)
        assert out.notna().all()
        assert (out == pd.Timestamp("2020-06-01")).all()

    def test_reads_nested_stats_block(self, cg):
        snap = {
            "kind": "datetime",
            "stats": {
                "min": "2020-06-01T00:00:00",
                "max": "2024-03-15T00:00:00",
                "year_bins": [{"year": 2021, "count": 1}],
            },
        }
        out = cg._generate_distribution_datetime(10, snap, SEED)
        assert out.notna().all()
        assert (out.dt.year == 2021).all()

"""D6a: distribution-driven numeric generation tests.

The contract D6a ships:

  - New `distribution` strategy on the generate op
  - Validator rejects missing snapshot dict + unknown kind
  - numeric kind samples from a {bin_edges, bin_counts} histogram
  - Output is deterministic for the same column_config (seed-driven)
  - Output's distribution matches the source (capstone via D5b
    compute_shape_fidelity score >= 0.95)
  - Edge cases: empty snapshot, malformed snapshot, single-bin
    constant column, zero-total counts

These tests pin the behavior every D6 follow-up (categorical D6b,
datetime D6c, the D7 privacy report) depends on.

Note on the config key. The operator-facing config uses
`strategy: distribution`. The generate op translates that to
`type: distribution` before handing the dict to ColumnGenerator
(see graph/ops/generate_op.py: `col_config.setdefault("type",
col_config.pop("strategy", "faker"))`). Tests that call
ColumnGenerator directly therefore use `type`, which is the
generator's actual contract; tests that go through `validate_config`
use `strategy`, which is the operator-facing surface.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from decoy_engine.errors import ValidationError
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.graph.ops.generate_op import validate_config


# ── op-level validator ─────────────────────────────────────────────────────


class TestValidator:
    def test_distribution_strategy_is_accepted(self):
        validate_config({
            "columns": {
                "age": {
                    "strategy": "distribution",
                    "snapshot": {
                        "kind": "numeric",
                        "bin_edges": [0, 10, 20],
                        "bin_counts": [5, 5],
                    },
                },
            },
        })

    def test_distribution_without_snapshot_dict_rejected(self):
        with pytest.raises(ValidationError):
            validate_config({
                "columns": {
                    "age": {"strategy": "distribution"},
                },
            })

    def test_distribution_with_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            validate_config({
                "columns": {
                    "age": {
                        "strategy": "distribution",
                        "snapshot": {"kind": "freetext"},
                    },
                },
            })

    def test_existing_strategies_still_accepted(self):
        """Regression guard: the new strategy + validator must not
        break the existing faker/sequence/categorical/formula paths."""
        for strat in ("faker", "sequence", "categorical", "formula"):
            validate_config({"columns": {"x": {"strategy": strat}}})


# ── numeric sampling ──────────────────────────────────────────────────────


def _gen() -> ColumnGenerator:
    return ColumnGenerator(seed=42)


class TestNumericSampling:
    def test_samples_within_bin_range(self):
        """Every output value falls inside [min(bin_edges), max(bin_edges)]."""
        gen = _gen()
        snap = {
            "kind": "numeric",
            "bin_edges": [0.0, 10.0, 20.0, 30.0],
            "bin_counts": [50, 100, 50],
        }
        series = gen.generate_column(
            num_rows=1000,
            column_config={
                "name": "x", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert len(series) == 1000
        assert series.min() >= 0.0
        assert series.max() <= 30.0

    def test_uniform_bin_weights_produces_uniform_distribution(self):
        """When every bin has the same count, output spreads roughly
        uniformly across the range."""
        gen = _gen()
        snap = {
            "kind": "numeric",
            "bin_edges": [0.0, 1.0, 2.0, 3.0, 4.0],
            "bin_counts": [100, 100, 100, 100],
        }
        series = gen.generate_column(
            num_rows=4000,
            column_config={
                "name": "u", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        # Bin each output back into the same edges and assert no bin
        # is wildly off the expected 1000.
        counts, _ = np.histogram(series.to_numpy(), bins=[0, 1, 2, 3, 4])
        for c in counts:
            # +/- 15% slack for 1000-sample noise.
            assert 850 <= c <= 1150, f"bin count {c} outside [850, 1150]"

    def test_weighted_distribution_concentrates_mass(self):
        """When one bin dominates, the output concentrates there."""
        gen = _gen()
        snap = {
            "kind": "numeric",
            "bin_edges": [0.0, 1.0, 2.0, 3.0],
            "bin_counts": [10, 900, 10],
        }
        series = gen.generate_column(
            num_rows=1000,
            column_config={
                "name": "w", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        counts, _ = np.histogram(series.to_numpy(), bins=[0, 1, 2, 3])
        # The middle bin should hold the vast majority of samples.
        assert counts[1] > 800, f"expected >800 in middle bin, got {counts[1]}"

    def test_determinism_same_config_same_output(self):
        """Same column_config (incl. snapshot) -> bitwise-identical output."""
        snap = {
            "kind": "numeric",
            "bin_edges": [0.0, 5.0, 10.0],
            "bin_counts": [50, 50],
        }
        cfg = {"name": "d", "type": "distribution", "snapshot": snap}
        s1 = _gen().generate_column(100, cfg, "t", {})
        s2 = _gen().generate_column(100, cfg, "t", {})
        pd.testing.assert_series_equal(s1, s2)

    def test_different_seeds_produce_different_output(self):
        """Two ColumnGenerators with different seeds -> different output."""
        snap = {
            "kind": "numeric",
            "bin_edges": [0.0, 100.0],
            "bin_counts": [1000],
        }
        cfg = {"name": "v", "type": "distribution", "snapshot": snap}
        s_a = ColumnGenerator(seed=1).generate_column(100, cfg, "t", {})
        s_b = ColumnGenerator(seed=2).generate_column(100, cfg, "t", {})
        # Some collisions are statistically possible but ~all-equal
        # would mean the seed isn't routing through. Assert at least
        # half the rows differ.
        assert (s_a != s_b).sum() > 50


# ── edge cases ─────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_constant_column_zero_range(self):
        """D1a's constant-column case: lo == hi, single bin. Output
        is the constant value `num_rows` times."""
        gen = _gen()
        snap = {
            "kind": "numeric",
            "bin_edges": [42.0, 42.0],
            "bin_counts": [10],
        }
        series = gen.generate_column(
            num_rows=50,
            column_config={
                "name": "c", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert (series == 42.0).all()

    def test_missing_bin_edges_emits_nulls(self):
        """Snapshot without bin_edges -> nulls + warning, no crash."""
        gen = _gen()
        snap = {"kind": "numeric"}
        series = gen.generate_column(
            num_rows=10,
            column_config={
                "name": "m", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert len(series) == 10
        assert series.isna().all()

    def test_length_mismatch_emits_nulls(self):
        """edges len must equal counts len + 1; off-by-one -> nulls."""
        gen = _gen()
        snap = {
            "kind": "numeric",
            "bin_edges": [0.0, 1.0],
            "bin_counts": [5, 5],
        }
        series = gen.generate_column(
            num_rows=5,
            column_config={
                "name": "n", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert series.isna().all()

    def test_zero_total_count_emits_nulls(self):
        gen = _gen()
        snap = {
            "kind": "numeric",
            "bin_edges": [0.0, 1.0, 2.0],
            "bin_counts": [0, 0],
        }
        series = gen.generate_column(
            num_rows=5,
            column_config={
                "name": "z", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert series.isna().all()

    def test_unknown_kind_emits_nulls(self):
        """Generator-level fallback for a kind the dispatcher doesn't
        handle (op validator catches this earlier, but the generator
        is defensive too)."""
        gen = _gen()
        snap = {"kind": "freetext"}
        series = gen.generate_column(
            num_rows=5,
            column_config={
                "name": "ft", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        assert series.isna().all()


# ── capstone: round-trip through shape_fidelity ───────────────────────────


class TestDistributionMatchesSource:
    """Real end-to-end test: take a real source column, snapshot it,
    feed the snapshot into the distribution generator, run the
    output back through compute_shape_fidelity vs the source. The
    D5b shape score should be ~1.0 because the generator's whole
    purpose is to reproduce the snapshot's distribution.

    This closes the measurement loop: D1 measures, D6 generates,
    D5b verifies."""

    def test_generated_column_matches_source_shape(self):
        from decoy_engine.quality.shape_fidelity import compute_shape_fidelity
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        # Build a realistic numeric column with a skewed distribution
        # (right-tailed: many young, few old).
        rng = np.random.default_rng(123)
        source = pd.DataFrame({
            "age": rng.gamma(shape=2.0, scale=15.0, size=5000).clip(0, 100),
        })
        src_snap = compute_distribution_snapshot(source)
        src_age_snap = src_snap["columns"]["age"]
        src_age_snap["kind"] = "numeric"  # surface for the generator

        # Generate 5000 rows from the source snapshot.
        gen = _gen()
        synth = gen.generate_column(
            num_rows=5000,
            column_config={
                "name": "age",
                "type": "distribution",
                "snapshot": src_age_snap,
            },
            table_name="t",
            reference_data={},
        )
        synth_df = pd.DataFrame({"age": synth})
        synth_snap = compute_distribution_snapshot(synth_df)

        shape = compute_shape_fidelity(src_snap, synth_snap)
        score = shape["marginal"]["columns"][0]["shape_similarity"]
        # The generator samples within the same bins the snapshot
        # carries; the resulting histogram has to match within
        # 1000-sample noise. >= 0.95 is the D5b "A-grade" boundary.
        assert score is not None
        assert score >= 0.95, (
            f"shape_similarity {score} below 0.95 - generator did not "
            "reproduce the source distribution within tolerance"
        )

    def test_synthetic_mean_close_to_source_mean(self):
        """Distribution-preserving generation should preserve summary
        statistics too (within sampling noise)."""
        rng = np.random.default_rng(456)
        source = pd.Series(
            rng.normal(loc=50, scale=10, size=4000).clip(0, 100),
        )
        snap = compute_distribution_snapshot(
            pd.DataFrame({"v": source}),
        )["columns"]["v"]
        snap["kind"] = "numeric"

        synth = _gen().generate_column(
            num_rows=4000,
            column_config={
                "name": "v", "type": "distribution", "snapshot": snap,
            },
            table_name="t",
            reference_data={},
        )
        # The mean of a Normal(50, 10) source should be ~50; the
        # generator emits uniform-within-bin samples so the mean
        # converges to source mean + bin discretization bias. With
        # 30 bins (D1a default) on a [0, 100] range the bias is small.
        src_mean = float(source.mean())
        synth_mean = float(synth.mean())
        assert abs(synth_mean - src_mean) < 2.0, (
            f"synth_mean {synth_mean} differs from source_mean "
            f"{src_mean} by more than 2.0"
        )


# Late import so the test module loads even when the engine quality
# package is the thing under construction.
from decoy_engine.quality.snapshot import compute_distribution_snapshot  # noqa: E402

"""QA-1 (2026-06-01) regression cells for ColumnGenerator RNG hardening.

Locks:
- H6: Two ColumnGenerators in the same process produce byte-identical
      output under the same seed regardless of what they did with their
      module-global random state (instance-local self._rng).
- H7: now/today/days_from_now/months_from_now/years_from_now read
      self._reference_date (snapshotted at construction time) instead
      of pd.Timestamp.now().
- M17: Two columns in the same row with different configs get
      different null masks.
- M18: synthetic_column_seed raises when derive_key fails (no silent
       fallback to seed-only path).
- M19: _generate_reference_column raises ValueError when
       reference_table is missing (no REF_TABLE_NOT_FOUND sentinel).
- M21: Two formula columns in the same job get independent RNG state
       (no module-global contamination between them).
- H9: PoolSampler raises GenerationError(code=
       'deterministic_mode_unsupported_cardinality') for mode=UNIQUE +
       deterministic=True.
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.generators.derivation import synthetic_column_seed


class TestQA1H6RngIsolation:
    def test_two_generators_same_seed_byte_identical(self):
        """H6: two ColumnGenerators constructed with the same seed
        produce byte-identical output for the same column config."""
        col = {"name": "id", "type": "sequence", "start": 1, "step": 1}
        cg_a = ColumnGenerator(seed=42)
        cg_b = ColumnGenerator(seed=42)
        out_a = cg_a.generate_column(10, col, "t", {})
        out_b = cg_b.generate_column(10, col, "t", {})
        assert out_a.tolist() == out_b.tolist()

    def test_module_global_random_pollution_does_not_change_output(self):
        """H6: a third party seeding module-global random.* BETWEEN
        ColumnGenerator constructions does not change either
        generator's output. Pre-fix this pattern was non-deterministic."""
        col = {"name": "cat", "type": "categorical",
               "categories": ["a", "b", "c"], "weights": [0.5, 0.3, 0.2]}
        cg_a = ColumnGenerator(seed=42)
        out_a = cg_a.generate_column(10, col, "t", {})
        # Pollute module-global random state between calls.
        random.seed(99999)
        for _ in range(100):
            random.random()
        cg_b = ColumnGenerator(seed=42)
        out_b = cg_b.generate_column(10, col, "t", {})
        assert out_a.tolist() == out_b.tolist()


class TestQA1H7ReferenceDate:
    def test_reference_date_default_snapshots_construction_time(self):
        """H7: when reference_date is not explicitly passed, the
        generator snapshots pd.Timestamp.utcnow() at construction."""
        cg = ColumnGenerator(seed=42)
        # The reference_date should be a pd.Timestamp.
        assert isinstance(cg._reference_date, pd.Timestamp)

    def test_reference_date_override_threads_to_formula_scope(self):
        """H7: when an explicit reference_date is passed, formulas
        using now()/today() return values based on it, not the wall
        clock."""
        ref = pd.Timestamp("2026-01-01")
        cg = ColumnGenerator(seed=42, reference_date=ref)
        col = {"name": "y", "type": "formula", "formula": "today()"}
        out = cg.generate_column(3, col, "t", {})
        assert all(v == "2026-01-01" for v in out.tolist())

    def test_two_generators_different_reference_dates_diverge(self):
        """H7: same formula + same seed + DIFFERENT reference_date
        produces different output. Locks that the date helpers
        actually read the param."""
        col = {"name": "y", "type": "formula", "formula": "today()"}
        cg_a = ColumnGenerator(seed=42, reference_date=pd.Timestamp("2026-01-01"))
        cg_b = ColumnGenerator(seed=42, reference_date=pd.Timestamp("2026-06-01"))
        out_a = cg_a.generate_column(1, col, "t", {})
        out_b = cg_b.generate_column(1, col, "t", {})
        assert out_a.tolist() != out_b.tolist()


class TestQA1M17NullSeedPerColumn:
    def test_two_columns_different_configs_get_different_null_masks(self):
        """M17: two faker columns with different `name` and different
        `faker_type` (i.e. different configs) at the same row index
        must NOT share a null/non-null decision. Pre-fix only the name
        flowed into the null seed."""
        col_a = {
            "name": "first_name", "type": "faker",
            "faker_type": "first_name", "null_probability": 0.5,
        }
        col_b = {
            "name": "last_name", "type": "faker",
            "faker_type": "last_name", "null_probability": 0.5,
        }
        cg = ColumnGenerator(seed=42)
        out_a = cg.generate_column(100, col_a, "t", {}).tolist()
        out_b = cg.generate_column(100, col_b, "t", {}).tolist()
        # Different null positions across the two columns.
        null_a_positions = {i for i, v in enumerate(out_a) if v is None or pd.isna(v)}
        null_b_positions = {i for i, v in enumerate(out_b) if v is None or pd.isna(v)}
        # Both columns had ~50% null probability; the masks must NOT be identical.
        assert null_a_positions != null_b_positions


class TestQA1M18DeriveKeyRaises:
    def test_synthetic_column_seed_raises_on_derive_key_failure(self):
        """M18: a failing derive_key in synthetic_column_seed must
        raise instead of silently falling through to the seed-only
        path."""
        def _boom(label):
            raise RuntimeError("simulated key resolver failure")

        col = {"name": "x", "type": "faker", "faker_type": "first_name"}
        with pytest.raises(RuntimeError, match="derive_key failed"):
            synthetic_column_seed(
                derive_key=_boom, column_config=col, fallback_seed=42,
            )


class TestQA1M19ReferenceTableRaises:
    def test_missing_reference_table_raises_value_error(self):
        """M19: a missing reference_table must raise ValueError, not
        return REF_TABLE_NOT_FOUND_N sentinel strings."""
        col = {
            "name": "fk", "type": "reference",
            "reference_table": "nonexistent",
            "reference_column": "id",
        }
        cg = ColumnGenerator(seed=42)
        with pytest.raises(ValueError, match="reference_table"):
            cg.generate_column(5, col, "t", {})

    def test_missing_reference_column_raises_value_error(self):
        """M19: a missing reference_column must raise ValueError, not
        return REF_COLUMN_NOT_FOUND_N sentinel strings."""
        col = {
            "name": "fk", "type": "reference",
            "reference_table": "parent",
            "reference_column": "nonexistent",
        }
        cg = ColumnGenerator(seed=42)
        ref_data = {"parent": pd.DataFrame({"id": [1, 2, 3]})}
        with pytest.raises(ValueError, match="reference_column"):
            cg.generate_column(5, col, "t", ref_data)


class TestQA1M21FormulaRngIsolation:
    def test_two_formula_columns_independent_rng(self):
        """M21: two formula columns in the same job do not share
        module-global RNG state. Column B's output is a pure function
        of (column_seed_b, row_index)."""
        col_a = {"name": "rand_a", "type": "formula", "formula": "randint(1, 1000)"}
        col_b = {"name": "rand_b", "type": "formula", "formula": "randint(1, 1000)"}

        # Run A then B in one generator.
        cg = ColumnGenerator(seed=42)
        out_a_then_b_a = cg.generate_column(10, col_a, "t", {}).tolist()
        out_a_then_b_b = cg.generate_column(10, col_b, "t", {}).tolist()

        # Run B then A in a fresh generator (different order).
        cg2 = ColumnGenerator(seed=42)
        out_b_then_a_b = cg2.generate_column(10, col_b, "t", {}).tolist()
        out_b_then_a_a = cg2.generate_column(10, col_a, "t", {}).tolist()

        # Column A's output must be the same regardless of whether B ran first.
        assert out_a_then_b_a == out_b_then_a_a
        # Same for B.
        assert out_a_then_b_b == out_b_then_a_b


class TestWalksGenF1ReferencePoolSortedDeterminism:
    """QA walks/generators F1 (2026-06-01, CRITICAL determinism):
    `_generate_reference_column` sorts the unique-values pool before
    sampling. Pre-fix the pool order came from
    `Series.dropna().unique().tolist()` which yields values in
    first-occurrence order. DB reads without ORDER BY produce
    undefined row order; the same seed could yield different FK
    assignments across runs."""

    def test_same_seed_different_ref_row_order_byte_identical(self):
        col = {
            "name": "fk",
            "type": "reference",
            "reference_table": "parent",
            "reference_column": "id",
        }
        ref_a = {"parent": pd.DataFrame({"id": [10, 20, 30, 40, 50]})}
        # Same values, different row order: pre-fix this produced a
        # different FK assignment under the same seed.
        ref_b = {"parent": pd.DataFrame({"id": [50, 30, 10, 40, 20]})}

        cg = ColumnGenerator(seed=42)
        out_a = cg.generate_column(50, col, "child", ref_a).tolist()

        cg2 = ColumnGenerator(seed=42)
        out_b = cg2.generate_column(50, col, "child", ref_b).tolist()

        assert out_a == out_b, (
            "QA walks/generators F1: FK assignment must be independent "
            f"of ref_df row order. Got {out_a[:10]}... vs {out_b[:10]}..."
        )

    def test_pool_with_string_values_sorted(self):
        """Mixed/string pools sort via the string fallback path."""
        col = {
            "name": "fk",
            "type": "reference",
            "reference_table": "parent",
            "reference_column": "code",
        }
        ref_a = {"parent": pd.DataFrame({"code": ["alpha", "beta", "gamma"]})}
        ref_b = {"parent": pd.DataFrame({"code": ["gamma", "alpha", "beta"]})}

        cg = ColumnGenerator(seed=42)
        out_a = cg.generate_column(20, col, "child", ref_a).tolist()
        cg2 = ColumnGenerator(seed=42)
        out_b = cg2.generate_column(20, col, "child", ref_b).tolist()
        assert out_a == out_b

    def test_walks_gen_f1_c1_mixed_type_pool_uses_str_fallback(self):
        """QA walks/generators F1 carry-1 (2026-06-01, MEDIUM):
        exercise the `except TypeError` fallback path. Mixed-type
        pools (int + str) trip Python's uniform-type sort and fall
        through to `sorted(..., key=str)`. Output must still be
        deterministic + identical across two ref_df orderings."""
        col = {
            "name": "fk",
            "type": "reference",
            "reference_table": "parent",
            "reference_column": "mixed",
        }
        ref_a = {"parent": pd.DataFrame({"mixed": [42, "alpha", 7, "beta", 3]})}
        ref_b = {"parent": pd.DataFrame({"mixed": ["beta", 7, 3, "alpha", 42]})}

        cg = ColumnGenerator(seed=42)
        out_a = cg.generate_column(20, col, "child", ref_a).tolist()
        cg2 = ColumnGenerator(seed=42)
        out_b = cg2.generate_column(20, col, "child", ref_b).tolist()
        assert out_a == out_b, (
            "QA walks/generators F1-c1: mixed-type pool must yield "
            "byte-identical FK output across different ref_df orderings."
        )

    def test_walks_gen_f1_c3_mixed_tz_datetime_pool_through_str_fallback(self):
        """QA walks/generators F1 carry-3 (2026-06-01, LOW):
        Mixed tz-aware + tz-naive datetimes in the pool. pandas raises
        TypeError comparing tz-aware to tz-naive Timestamps, which
        trips the str-fallback path. The fallback sorts by string
        repr (stable + deterministic) so the FK output is byte-stable
        across pool orderings."""
        ts_naive_a = pd.Timestamp("2026-01-01")
        ts_naive_b = pd.Timestamp("2026-03-15")
        ts_aware_a = pd.Timestamp("2026-02-01", tz="UTC")
        ts_aware_b = pd.Timestamp("2026-04-15", tz="UTC")

        col = {
            "name": "fk",
            "type": "reference",
            "reference_table": "parent",
            "reference_column": "ts",
        }
        ref_a = {"parent": pd.DataFrame({"ts": [ts_naive_a, ts_aware_a, ts_naive_b, ts_aware_b]})}
        ref_b = {"parent": pd.DataFrame({"ts": [ts_aware_b, ts_naive_a, ts_aware_a, ts_naive_b]})}

        cg = ColumnGenerator(seed=42)
        out_a = cg.generate_column(20, col, "child", ref_a).tolist()
        cg2 = ColumnGenerator(seed=42)
        out_b = cg2.generate_column(20, col, "child", ref_b).tolist()
        assert out_a == out_b, (
            "QA walks/generators F1-c3: mixed tz-aware + tz-naive "
            "datetime pool must yield byte-identical FK output across "
            "different ref_df orderings via the str-fallback path."
        )

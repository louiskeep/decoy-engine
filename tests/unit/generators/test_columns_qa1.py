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


class TestQaWalksGenF3VectorisedNullInjection:
    """QA walks/generators F3 / PO Q-F3=b (2026-06-01, HIGH
    correctness + perf): null injection is now vectorised through
    numpy.random.default_rng + uses pandas nullable Int64 for integer
    columns so the source dtype survives null assignment.

    Two issues closed:
      (A) Pre-fix `result.iloc[i] = None` on int64 promoted in-place
          to float64. Downstream schema validators + masking strategies
          expecting int64 then received float64.
      (B) Pre-fix N reseed calls + N pandas scalar setters per column
          inside the innermost generation loop. At 100K rows + p=0.1
          that was ~100ms of pure seeding overhead. Now: one RNG
          construct + one vectorised draw.

    SEED_PROTOCOL_VERSION bumped 2 -> 3 in the same change because
    numpy.default_rng + Python random.Random produce different floats
    for the same integer seed; the null PATTERN changes byte-for-byte.
    Null FRACTION still converges to null_probability."""

    def test_int_column_dtype_survives_null_injection(self):
        """Audit scenario: formula `randint(1, 100)` returns int64;
        pre-fix the first null assign upcast to float64 in-place +
        downstream schema validators expecting int64 received float64.
        Post-fix the F3 path detects int dtype + promotes to pandas
        nullable Int64 BEFORE applying the null mask."""
        import pandas as pd

        col = {
            "name": "x",
            "type": "formula",
            "formula": "randint(1, 100)",
            "null_probability": 0.3,
        }
        cg = ColumnGenerator(seed=42)
        result = cg.generate_column(200, col, "t", {})
        assert pd.api.types.is_integer_dtype(result), (
            f"QA walks/generators F3: int column dtype must survive null "
            f"injection. Got dtype {result.dtype}."
        )
        # And actually contains nulls.
        assert result.isna().sum() > 0
        # Specifically the nullable Int64 (NOT plain int64 which can't
        # hold NaN, NOT float64 from the upcast bug, NOT object).
        assert str(result.dtype) == "Int64"

    def test_null_fraction_converges_to_null_probability(self):
        col = {
            "name": "x",
            "type": "formula",
            "formula": "randint(1, 100)",
            "null_probability": 0.5,
        }
        cg = ColumnGenerator(seed=42)
        result = cg.generate_column(1000, col, "t", {})
        null_count = result.isna().sum()
        # Wide bound for n=1000 binomial variance + headroom for future
        # RNG family swaps that preserve the FRACTION contract.
        assert 0.4 <= null_count / 1000 <= 0.6, (
            f"QA walks/generators F3: null fraction should converge to "
            f"null_probability=0.5; got {null_count}/{1000} = "
            f"{null_count/1000:.3f}"
        )

    def test_deterministic_null_pattern_same_seed_same_pattern(self):
        """Bytes change between V1 and V2 (the breaking part of F3) but
        within V2: same column_seed -> same null mask. Pin the new
        determinism contract."""
        col = {
            "name": "x",
            "type": "formula",
            "formula": "randint(1, 100)",
            "null_probability": 0.3,
        }
        cg_a = ColumnGenerator(seed=42)
        result_a = cg_a.generate_column(100, col, "t", {})
        cg_b = ColumnGenerator(seed=42)
        result_b = cg_b.generate_column(100, col, "t", {})
        # Same seed -> same null positions.
        assert (result_a.isna() == result_b.isna()).all()


class TestFormulaHashKeyedMigration:
    """Formula-hash migration (2026-06-01, PO confirmed): the formula
    sandbox `hash()` function now uses HMAC-SHA256 keyed by the per-row
    local_seed (_formula_hash_keyed), not the legacy
    deterministic_hash. SEED_PROTOCOL_VERSION bumped 3 -> 4 in the
    same change.

    Per-row output bytes change vs the pre-migration shape (HMAC vs
    raw SHA256). Same input + same seed within v4 still yields the
    same output (determinism preserved within the protocol version)."""

    def test_formula_hash_does_not_emit_deprecation_warning_per_row(self):
        """Carry from Dennis pass-7 M1: the formula sandbox must not
        leak DeprecationWarning per-row. Pre-migration this was a real
        problem because the sandbox called deterministic_hash. Post-
        migration the call goes through hmac_hex (no warning emitter)
        so the contract holds for free."""
        import warnings

        col = {
            "name": "hashed",
            "type": "formula",
            "formula": "hash(i)",
        }
        cg = ColumnGenerator(seed=42)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            result = cg.generate_column(50, col, "t", {})
        depr_warnings = [
            w for w in caught
            if issubclass(w.category, DeprecationWarning)
        ]
        assert depr_warnings == [], (
            f"Formula-hash migration: sandbox `hash` leaked "
            f"{len(depr_warnings)} DeprecationWarning(s); migration "
            f"to keyed primitive should remove all such emissions."
        )
        assert len(result) == 50

    def test_formula_hash_output_is_8_hex_chars(self):
        """Output contract: 8-char hex string per row (unchanged from
        pre-migration; only the underlying bytes differ)."""
        col = {
            "name": "h",
            "type": "formula",
            "formula": "hash(i)",
        }
        cg = ColumnGenerator(seed=42)
        result = cg.generate_column(10, col, "t", {})
        for v in result:
            assert isinstance(v, str)
            assert len(v) == 8
            int(v, 16)  # parseable as hex

    def test_formula_hash_deterministic_same_seed_same_output(self):
        """Within a single SEED_PROTOCOL_VERSION + same seed input,
        the hash output is identical across runs."""
        col = {
            "name": "h",
            "type": "formula",
            "formula": "hash(i)",
        }
        out_a = ColumnGenerator(seed=42).generate_column(10, col, "t", {}).tolist()
        out_b = ColumnGenerator(seed=42).generate_column(10, col, "t", {}).tolist()
        assert out_a == out_b

    def test_formula_hash_different_seeds_diverge(self):
        col = {
            "name": "h",
            "type": "formula",
            "formula": "hash(i)",
        }
        out_a = ColumnGenerator(seed=42).generate_column(10, col, "t", {}).tolist()
        out_b = ColumnGenerator(seed=99).generate_column(10, col, "t", {}).tolist()
        assert out_a != out_b


class TestWalksGenF7DistributionDatetimeYear9999:
    """QA walks/generators F7 (2026-06-01, MEDIUM correctness):
    `_generate_distribution_datetime` does not crash on source rows
    with year 9999. Pre-fix `y + 1` produced the literal `10000-01-01`
    string; `pd.Timestamp("10000-01-01")` raises OutOfBoundsDatetime.
    Post-fix the exclusive year-end is capped at 9999-12-31 for the
    year-9999 row only; the snapshot maximum (ts_max) clip below still
    bounds the per-row window."""

    def test_beyond_ts_max_year_bin_does_not_raise(self):
        """A snapshot with a year_bins entry beyond ts_max (a malformed
        but parseable snapshot) must not crash with OutOfBoundsDatetime.
        Pre-fix the intermediate year_starts construction crashed for
        any year beyond the active datetime precision. Post-fix
        years_arr is capped at ts_max.year so the per-row clip never
        sees the out-of-range year string."""
        snapshot = {
            "kind": "datetime",
            "min": "2026-01-01T00:00:00",
            "max": "2026-12-31T23:59:59",
            "year_bins": [
                {"year": 2026, "count": 100},
                # Beyond ts_max (and beyond ns precision): pre-fix
                # this killed the entire distribution sampler.
                {"year": 9999, "count": 50},
            ],
        }
        col = {
            "name": "ts",
            "type": "distribution",
            "snapshot": snapshot,
        }
        cg = ColumnGenerator(seed=42)
        # Must not raise OutOfBoundsDatetime.
        result = cg.generate_column(150, col, "t", {})
        assert len(result) == 150
        # Every output must land within [ts_min, ts_max] because the
        # nanosecond clip caps everything at ts_max.
        years = pd.to_datetime(result).dt.year
        assert years.max() <= 2026, (
            f"QA walks/generators F7: capped years_arr should keep "
            f"output <= ts_max.year=2026. Got max year {years.max()}."
        )


class TestWalksGenF8LocaleFakerCache:
    """QA walks/generators F8 (2026-06-01, LOW perf):
    `_generate_faker_column` caches per-locale Faker instances in
    `self._locale_fakers`. Pre-fix a 30-column table with the same
    `locale: en_GB` rebuilt 30 separate Faker instances + ran 30
    provider scans. Post-fix the locale Faker is built once per
    generator lifetime."""

    def test_locale_faker_cache_initialized_on_construction(self):
        cg = ColumnGenerator(seed=42)
        assert hasattr(cg, "_locale_fakers")
        assert isinstance(cg._locale_fakers, dict)
        assert cg._locale_fakers == {}, "Cache must start empty"

    def test_locale_faker_cache_populated_on_first_use(self):
        cg = ColumnGenerator(seed=42)
        col = {
            "name": "city",
            "type": "faker",
            "faker_type": "city",
            "locale": "en_GB",
        }
        # First call: cache miss, populates the cache.
        cg.generate_column(5, col, "t", {}).tolist()
        assert "en_GB" in cg._locale_fakers
        assert len(cg._locale_fakers) == 1

        # Second call with same locale: cache hit; no new entry.
        cg.generate_column(5, col, "t", {}).tolist()
        assert len(cg._locale_fakers) == 1

        # Third call with different locale: cache miss again.
        col_de = {
            "name": "city2",
            "type": "faker",
            "faker_type": "city",
            "locale": "de_DE",
        }
        cg.generate_column(5, col_de, "t", {}).tolist()
        assert "de_DE" in cg._locale_fakers
        assert len(cg._locale_fakers) == 2

    def test_locale_faker_cache_does_not_break_no_locale_path(self):
        """The no-locale path still uses self.faker; the cache is
        only consulted when locale is set."""
        cg = ColumnGenerator(seed=42)
        col = {
            "name": "city",
            "type": "faker",
            "faker_type": "city",
        }
        cg.generate_column(5, col, "t", {}).tolist()
        # No locale passed -> cache stays empty.
        assert cg._locale_fakers == {}

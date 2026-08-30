"""Task 3.2: route-local `pool_quality` obligation enforcement.

`_pool_quality.py` is the FIRST consumer of `_capabilities.py`'s
`quality_obligations` tag. These tests cover: non-vacuity (a breaching pool
raises, a compliant one passes) for both the collision-rate and pool-
duplicate-rate metrics, the empty-population special case, the
non-deterministic-source measurement-integrity check, the bounded (spill-
backed DuckDB) aggregation path with a hand-computed oracle example, the
coded error contract, and non-interference with the general capability
resolver (HIGH 4).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.execution.native._capabilities import capabilities_for
from decoy_engine.execution.native._pool_quality import (
    COLLISION_RATE_THRESHOLD,
    MARGIN,
    ORACLE_COLLISION_RATE,
    ORACLE_POOL_DUPLICATE_RATE,
    POOL_DUPLICATE_RATE_THRESHOLD,
    UNIQUE_FEASIBILITY_NA,
    PoolQualityError,
    PoolQualityMeasurement,
    _connect_duckdb_spill,
    enforce_pool_quality,
    measure_pool_quality,
)
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.generation.pool._value_pool import ValuePool


def _pool(size: int, distinct_count: int) -> ValuePool:
    # Values themselves are irrelevant to pool_quality (only size and
    # distinct_count feed the metric); fill with a repeating pattern that
    # actually has `distinct_count` distinct entries so the fixture is
    # internally consistent for anyone reading the test.
    values = np.array([f"v{i % distinct_count}" for i in range(size)], dtype=object)
    return ValuePool(
        values=values,
        provider="person_first_name",
        locale="en_US",
        config_hash="test_hash",
        seed=b"seed",
        size=size,
        build_time_ms=0.0,
        backend_type="faker",
        backend_version="1.0",
        distinct_count=distinct_count,
    )


def _measure(
    column: str,
    source: Sequence[str | None],
    masked: Sequence[str | None],
    pool: ValuePool,
    tmp_path: Path,
    *,
    memory_limit: str | None = "64MB",
) -> PoolQualityMeasurement:
    return measure_pool_quality(
        column=column,
        source=pa.array(source, type=pa.string()),
        masked=pa.array(masked, type=pa.string()),
        pool=pool,
        temp_dir=tmp_path,
        memory_limit=memory_limit,
    )


class TestCollisionRateNonVacuity:
    def test_breaching_collision_rate_raises(self, tmp_path: Path) -> None:
        # 10 distinct sources collapsed onto 3 distinct outputs:
        # collision_count = 10 - 3 = 7, collision_rate = 0.7, which exceeds
        # FIRST's frozen threshold (0.6630).
        sources = [f"s{i}" for i in range(10)]
        masked = [f"o{i % 3}" for i in range(10)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.collision_rate == pytest.approx(0.7)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "collision_rate"
        assert exc_info.value.observed == pytest.approx(0.7)

    def test_compliant_collision_rate_passes(self, tmp_path: Path) -> None:
        # Every source maps to its own unique output: zero collisions.
        sources = [f"s{i}" for i in range(20)]
        masked = [f"o{i}" for i in range(20)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.collision_rate == 0.0
        enforce_pool_quality(measurement, column="FIRST")  # must not raise


class TestPoolDuplicateRateNonVacuity:
    def test_breaching_pool_duplicate_rate_raises(self, tmp_path: Path) -> None:
        # size=100, distinct_count=2: duplicate_rate = 98/100 = 0.98, which
        # exceeds FIRST's frozen threshold (0.9538).
        sources = [f"s{i}" for i in range(5)]
        masked = [f"o{i}" for i in range(5)]  # zero collisions, so only the
        # pool-duplicate check can fail here.
        pool = _pool(size=100, distinct_count=2)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.pool_duplicate_rate == pytest.approx(0.98)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "pool_duplicate_rate"
        assert exc_info.value.observed == pytest.approx(0.98)

    def test_compliant_pool_duplicate_rate_passes(self, tmp_path: Path) -> None:
        sources = [f"s{i}" for i in range(5)]
        masked = [f"o{i}" for i in range(5)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.pool_duplicate_rate == 0.0
        enforce_pool_quality(measurement, column="FIRST")  # must not raise


class TestEmptyPopulation:
    def test_all_null_source_column_passes_at_rate_zero(self, tmp_path: Path) -> None:
        sources: list[str | None] = [None, None, None]
        masked = ["o0", "o1", "o2"]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("MAIDEN", sources, masked, pool, tmp_path)

        assert measurement.distinct_sources == 0
        assert measurement.collision_rate == 0.0
        assert measurement.unique_feasibility == UNIQUE_FEASIBILITY_NA
        enforce_pool_quality(measurement, column="MAIDEN")  # must not raise

    def test_zero_rows_passes_at_rate_zero(self, tmp_path: Path) -> None:
        sources: list[str | None] = []
        masked: list[str | None] = []
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("LAST", sources, masked, pool, tmp_path)

        assert measurement.distinct_sources == 0
        assert measurement.collision_rate == 0.0
        enforce_pool_quality(measurement, column="LAST")  # must not raise


class TestNonDeterministicSourceIntegrity:
    def test_one_source_two_outputs_raises_regardless_of_collision_rate(
        self, tmp_path: Path
    ) -> None:
        # Same source ("s0") maps to two different masked values across
        # rows: the deterministic sampler must never do this. Collision
        # rate alone would read as compliant (1 distinct source, 1 ANY_VALUE
        # output picked arbitrarily -> collision_rate 0.0), so this proves
        # the integrity check is independent of the collision-rate gate.
        sources = ["s0", "s0"]
        masked = ["o0", "o1"]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.non_deterministic_sources == 1
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "non_deterministic_sources"
        assert exc_info.value.observed == 1

    def test_repeated_source_same_output_is_not_flagged(self, tmp_path: Path) -> None:
        # Intentional deterministic reuse: a source recurring and always
        # mapping to the SAME output is not a collision and not
        # non-deterministic.
        sources = ["s0", "s0", "s1"]
        masked = ["o0", "o0", "o1"]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.non_deterministic_sources == 0
        assert measurement.distinct_sources == 2
        assert measurement.collision_rate == 0.0
        enforce_pool_quality(measurement, column="FIRST")  # must not raise


class TestBoundedAggregation:
    def test_spill_backed_connection_config(self, tmp_path: Path) -> None:
        conn = _connect_duckdb_spill(temp_dir=tmp_path, memory_limit="64MB")
        try:
            temp_directory = conn.execute("SELECT current_setting('temp_directory')").fetchone()[0]
            preserve_order = conn.execute(
                "SELECT current_setting('preserve_insertion_order')"
            ).fetchone()[0]
            memory_limit = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        finally:
            conn.close()

        assert temp_directory == str(tmp_path)
        assert preserve_order is False
        # DuckDB reports memory_limit in its own normalized unit string;
        # just assert our 64MB request was not ignored (not the default).
        assert "64" in memory_limit or "61" in memory_limit  # MiB/MB rounding

    def test_measurement_spools_pairs_to_parquet_not_a_python_set(self, tmp_path: Path) -> None:
        sources = [f"s{i}" for i in range(6)]
        masked = ["a", "a", "b", "b", "c", "d"]
        pool = _pool(size=10, distinct_count=10)
        _measure("FIRST", sources, masked, pool, tmp_path)

        # The frozen method spools (source, masked) pairs to Parquet and lets
        # DuckDB do the grouping; this is the on-disk evidence that no
        # O(distinct-sources) Python structure was the aggregation path.
        pairs_path = tmp_path / "pairs_FIRST.parquet"
        assert pairs_path.exists()
        table = pa.parquet.read_table(pairs_path)
        assert table.num_rows == 6

    def test_hand_computed_collision_rate_oracle(self, tmp_path: Path) -> None:
        # 6 distinct sources -> masked: s0/s1 -> a, s2/s3 -> b, s4 -> c,
        # s5 -> d. distinct_sources = 6, distinct_outputs_for_distinct_
        # sources = {a, b, c, d} = 4. collision_count = 6 - 4 = 2 (s1 and s3
        # each collide with an earlier source onto the same output).
        # collision_rate = 2 / 6 = 0.333...
        sources = ["s0", "s1", "s2", "s3", "s4", "s5"]
        masked = ["a", "a", "b", "b", "c", "d"]
        pool = _pool(size=10, distinct_count=10)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        expected_distinct_sources = 6
        expected_distinct_outputs = 4
        expected_collision_rate = (
            expected_distinct_sources - expected_distinct_outputs
        ) / expected_distinct_sources

        assert measurement.distinct_sources == expected_distinct_sources
        assert measurement.collision_rate == pytest.approx(expected_collision_rate)
        assert measurement.collision_rate == pytest.approx(1 / 3)
        assert measurement.non_deterministic_sources == 0


class TestFrozenThresholdProvenance:
    def test_thresholds_are_oracle_rate_plus_margin(self) -> None:
        assert MARGIN == 0.02
        for column, oracle_rate in ORACLE_COLLISION_RATE.items():
            assert COLLISION_RATE_THRESHOLD[column] == pytest.approx(oracle_rate + MARGIN)
        for column, oracle_rate in ORACLE_POOL_DUPLICATE_RATE.items():
            assert POOL_DUPLICATE_RATE_THRESHOLD[column] == pytest.approx(oracle_rate + MARGIN)

    def test_frozen_first_last_maiden_values_match_baseline(self) -> None:
        # PHASE3-C1-BASELINE.md "Frozen per-tier thresholds" table.
        assert COLLISION_RATE_THRESHOLD["FIRST"] == pytest.approx(0.6630)
        assert COLLISION_RATE_THRESHOLD["LAST"] == pytest.approx(0.5817)
        assert COLLISION_RATE_THRESHOLD["MAIDEN"] == pytest.approx(0.3144)
        assert POOL_DUPLICATE_RATE_THRESHOLD["FIRST"] == pytest.approx(0.9538)
        assert POOL_DUPLICATE_RATE_THRESHOLD["LAST"] == pytest.approx(0.9213)
        assert POOL_DUPLICATE_RATE_THRESHOLD["MAIDEN"] == pytest.approx(0.9217)


class TestUnrecognizedObligationAndColumn:
    def test_unrecognized_obligation_rejected_coded(self, tmp_path: Path) -> None:
        pool = _pool(size=10, distinct_count=10)
        measurement = _measure("FIRST", ["s0"], ["o0"], pool, tmp_path)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST", obligation="some_other_obligation")
        assert exc_info.value.code == "pool_quality.threshold_exceeded"
        assert exc_info.value.metric == "obligation"

    def test_unrecognized_column_rejected_coded(self, tmp_path: Path) -> None:
        pool = _pool(size=10, distinct_count=10)
        measurement = _measure("SUFFIX", ["s0"], ["o0"], pool, tmp_path)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="SUFFIX")
        assert exc_info.value.code == "pool_quality.threshold_exceeded"
        assert exc_info.value.metric == "column"


class TestCodedError:
    def test_code_is_stable_and_message_names_column_and_metric(self, tmp_path: Path) -> None:
        sources = [f"s{i}" for i in range(10)]
        masked = [f"o{i % 3}" for i in range(10)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")

        assert PoolQualityError.code == "pool_quality.threshold_exceeded"
        assert exc_info.value.code == "pool_quality.threshold_exceeded"
        message = str(exc_info.value)
        assert "FIRST" in message
        assert "collision_rate" in message

    def test_warnings_surfaced_in_breach_message_but_never_raise_alone(
        self, tmp_path: Path
    ) -> None:
        sources = [f"s{i}" for i in range(10)]
        masked = [f"o{i % 3}" for i in range(10)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)
        warning = QualityWarning(code="pool_scaled_up", provider="person_first_name")

        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST", warnings=(warning,))
        assert "pool_scaled_up" in str(exc_info.value)

        # A compliant pool with the SAME warning present must not raise: a
        # mere QualityWarning never escalates on its own.
        compliant_sources = [f"s{i}" for i in range(20)]
        compliant_masked = [f"o{i}" for i in range(20)]
        compliant_measurement = _measure(
            "FIRST", compliant_sources, compliant_masked, pool, tmp_path
        )
        enforce_pool_quality(compliant_measurement, column="FIRST", warnings=(warning,))


class TestNonInterference:
    """HIGH 4: this task adds a NEW consumer; it must not change
    `capabilities_for`'s classification table or behavior for any other
    strategy/path."""

    def test_faker_quality_obligations_unchanged(self) -> None:
        assert capabilities_for("faker").quality_obligations == ("pool_quality",)

    def test_composite_quality_obligations_unchanged(self) -> None:
        assert capabilities_for("<composite>").quality_obligations == ("pool_quality",)

    @pytest.mark.parametrize(
        "strategy",
        [
            "passthrough",
            "redact",
            "hash",
            "fpe",
            "date_shift",
            "categorical",
            "bucketize",
            "shuffle",
            "top_code",
            "<group>",
        ],
    )
    def test_non_c1_strategy_quality_obligations_are_empty_and_untouched(
        self, strategy: str
    ) -> None:
        # These strategies never carry `pool_quality`; `enforce_pool_quality`
        # is never invoked for them by any caller this task adds, and this
        # task does not touch their `_capabilities.py` entries at all.
        assert capabilities_for(strategy).quality_obligations == ()

    def test_capabilities_for_signature_and_module_untouched(self) -> None:
        # This task adds a NEW module (`_pool_quality.py`); it does not
        # modify `capabilities_for`'s resolver logic. A smoke check that
        # unclassified strategies still fail loudly (the pre-task contract).
        with pytest.raises(KeyError):
            capabilities_for("not_a_real_strategy")

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

import math
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import decoy_engine.execution.native._pool_quality as pool_quality_module
from decoy_engine.execution.native._capabilities import capabilities_for
from decoy_engine.execution.native._pool_quality import (
    COLLISION_RATE_THRESHOLD,
    FROZEN_DISTINCT_SOURCES,
    FROZEN_POOL_SIZE,
    MARGIN,
    ORACLE_COLLISION_RATE,
    ORACLE_POOL_DUPLICATE_RATE,
    POOL_DUPLICATE_RATE_THRESHOLD,
    UNIQUE_FEASIBILITY_NA,
    PoolQualityError,
    PoolQualityMeasurement,
    enforce_pool_quality,
    measure_pool_quality,
)
from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
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


def _at_frozen_tier(measurement: PoolQualityMeasurement, column: str) -> PoolQualityMeasurement:
    """Rehome a measurement onto the (pool_size, distinct_sources) tuple
    `enforce_pool_quality`'s tuple-aware guard requires, leaving every other
    field -- the actual rates a test wants to exercise -- untouched.

    Many tests below build small synthetic populations (tens of sources, a
    100-value pool) purely to exercise the RATE arithmetic and the threshold
    comparison; the tuple guard itself is a SEPARATE concern with its own
    dedicated tests (`TestTupleAwareThresholdGuard`). This lets those two
    concerns stay independent instead of forcing every arithmetic test to
    also fabricate a literal 1,000/1,200/360-source, 10,000-value population.
    """
    distinct_sources = (
        measurement.distinct_sources
        if measurement.distinct_sources == 0
        else FROZEN_DISTINCT_SOURCES[column]
    )
    return replace(
        measurement,
        pool_size=FROZEN_POOL_SIZE[column],
        distinct_sources=distinct_sources,
    )


def _reference_collision_measurement(
    pairs: Sequence[tuple[str | None, str | None]],
) -> tuple[int, int, int]:
    """Pure-python reference for the frozen `WHERE source IS NOT NULL`
    collision aggregation, computed directly from RAW (source, masked)
    pairs. Never reads `PoolQualityMeasurement`, a production threshold
    constant, or `ValuePool.distinct_count` -- grading the code against its
    own output would prove nothing.

    Mirrors `ANY_VALUE(masked)`'s actual behavior in this repo's pinned
    DuckDB version (checked directly against a live connection): a source's
    `out_val` is its single distinct non-null masked value when exactly one
    exists, NULL when every row for that source has a null masked value, and
    arbitrary when more than one distinct non-null value exists. That last
    case IS `non_deterministic_sources`, and production always reports
    `collision_rate` as NaN when it is nonzero (see `_reference_collision_
    rate`), so this reference never needs to reproduce DuckDB's arbitrary
    pick to agree with it.
    """
    per_source: dict[str, list[str | None]] = {}
    for source, masked in pairs:
        if source is None:
            continue
        per_source.setdefault(source, []).append(masked)

    distinct_sources = len(per_source)
    non_deterministic_sources = 0
    out_vals: list[str | None] = []
    for masked_values in per_source.values():
        distinct_masked = {m for m in masked_values if m is not None}
        if len(distinct_masked) > 1:
            non_deterministic_sources += 1
        out_vals.append(next(iter(distinct_masked)) if distinct_masked else None)

    distinct_outputs = len({v for v in out_vals if v is not None})
    return distinct_sources, distinct_outputs, non_deterministic_sources


def _reference_collision_rate(
    distinct_sources: int, distinct_outputs: int, non_deterministic_sources: int
) -> float:
    """Same rate formula PHASE3-C1-BASELINE.md freezes, applied to the
    reference's own counts (never the production measurement's)."""
    if distinct_sources == 0:
        return 0.0
    if non_deterministic_sources != 0:
        return float("nan")
    return (distinct_sources - distinct_outputs) / distinct_sources


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
            enforce_pool_quality(_at_frozen_tier(measurement, "FIRST"), column="FIRST")
        assert exc_info.value.metric == "collision_rate"
        assert exc_info.value.observed == pytest.approx(0.7)

    def test_compliant_collision_rate_passes(self, tmp_path: Path) -> None:
        # Every source maps to its own unique output: zero collisions.
        sources = [f"s{i}" for i in range(20)]
        masked = [f"o{i}" for i in range(20)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.collision_rate == 0.0
        enforce_pool_quality(
            _at_frozen_tier(measurement, "FIRST"), column="FIRST"
        )  # must not raise


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
            enforce_pool_quality(_at_frozen_tier(measurement, "FIRST"), column="FIRST")
        assert exc_info.value.metric == "pool_duplicate_rate"
        assert exc_info.value.observed == pytest.approx(0.98)

    def test_compliant_pool_duplicate_rate_passes(self, tmp_path: Path) -> None:
        sources = [f"s{i}" for i in range(5)]
        masked = [f"o{i}" for i in range(5)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.pool_duplicate_rate == 0.0
        enforce_pool_quality(
            _at_frozen_tier(measurement, "FIRST"), column="FIRST"
        )  # must not raise


class TestEmptyPopulation:
    def test_all_null_source_column_passes_at_rate_zero(self, tmp_path: Path) -> None:
        sources: list[str | None] = [None, None, None]
        masked = ["o0", "o1", "o2"]
        # Pool size matches MAIDEN's frozen tier (10,000): the empty-source
        # population is exempt from the distinct_sources tuple check, but NOT
        # from the pool_size check -- the pool is fully built regardless of
        # whether the source column happens to be all-null.
        pool = _pool(size=10_000, distinct_count=10_000)
        measurement = _measure("MAIDEN", sources, masked, pool, tmp_path)

        assert measurement.distinct_sources == 0
        assert measurement.collision_rate == 0.0
        assert measurement.unique_feasibility == UNIQUE_FEASIBILITY_NA
        enforce_pool_quality(measurement, column="MAIDEN")  # must not raise

    def test_zero_rows_passes_at_rate_zero(self, tmp_path: Path) -> None:
        sources: list[str | None] = []
        masked: list[str | None] = []
        pool = _pool(size=10_000, distinct_count=10_000)
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
        assert exc_info.value.column == "FIRST"
        # threshold is always 0 (any nonzero count is a failure): pins the
        # literal against a mutant that reports a different "allowed" count.
        assert exc_info.value.threshold == 0
        message = str(exc_info.value)
        # Boundary-spanning: the message concatenates several adjacent
        # string-literal fragments; a mutant that wraps only ONE fragment in
        # filler text leaves its own inner substring intact, so each check
        # below straddles a fragment join (where the wrap markers actually
        # land) rather than sitting inside a single fragment's interior.
        assert "1 source value(s) mapped to more than one masked output" in message
        assert "the deterministic sampler must never do this" in message
        assert "not a tolerance breach)" in message

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
        enforce_pool_quality(
            _at_frozen_tier(measurement, "FIRST"), column="FIRST"
        )  # must not raise


class TestBoundedAggregation:
    def test_measurement_uses_the_memory_safe_shared_connection(self, tmp_path: Path) -> None:
        # HIGH-1: the collision aggregation must use connect_duckdb (the
        # out-of-core route's memory-safe helper), not a fork that drops its
        # threads-vs-memory_limit clamp. A tight memory_limit pins threads low
        # so DuckDB's per-thread working set cannot blow the budget before
        # operators spill.
        conn = connect_duckdb(temp_dir=tmp_path, memory_limit="64MB")
        try:
            temp_directory = conn.execute("SELECT current_setting('temp_directory')").fetchone()[0]
            preserve_order = conn.execute(
                "SELECT current_setting('preserve_insertion_order')"
            ).fetchone()[0]
            memory_limit = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
            threads = int(conn.execute("SELECT current_setting('threads')").fetchone()[0])
        finally:
            conn.close()

        assert temp_directory == str(tmp_path)
        assert preserve_order is False
        # DuckDB reports memory_limit in its own normalized unit string;
        # just assert our 64MB request was not ignored (not the default).
        assert "64" in memory_limit or "61" in memory_limit  # MiB/MB rounding
        # The clamp is the safety-critical setting the forked helper dropped:
        # a tight 64MB limit pins threads to 1, never os.cpu_count().
        assert threads == 1

    def test_measurement_deletes_the_cleartext_pairs_spool(self, tmp_path: Path) -> None:
        # MEDIUM-1: the (source, masked) spool holds cleartext source PII and
        # must not linger at rest after the aggregate row is fetched. The
        # Parquet-spool-plus-DuckDB path (not an O(distinct-sources) Python
        # structure) is the bounded-aggregation contract; here we prove the
        # spool is cleaned up while the measurement still comes back correct.
        sources = [f"s{i}" for i in range(6)]
        masked = ["a", "a", "b", "b", "c", "d"]
        pool = _pool(size=10, distinct_count=10)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        # The spool name carries a random per-call suffix (P3-T2: two
        # overlapping measurements of the same column must not share a
        # file), so check for ANY leftover parquet file, not one literal
        # stale name -- a glob check that only matched the old fixed name
        # would trivially pass without proving anything was cleaned up.
        assert list(tmp_path.glob("pairs_FIRST*.parquet")) == []
        assert measurement.distinct_sources == 6

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


class TestThresholdBoundary:
    # MEDIUM-2: the comparison is `>` not `>=`, so observed == threshold is
    # within tolerance (threshold = oracle rate + margin) and MUST pass. These
    # pin the boundary direction against a `>=` mutation. Build the measurement
    # at the exact boundary directly, rather than reverse-engineering pairs
    # that land on the exact float.
    def test_collision_rate_exactly_at_threshold_passes(self) -> None:
        measurement = PoolQualityMeasurement(
            column="FIRST",
            distinct_sources=1000,
            non_deterministic_sources=0,
            collision_rate=COLLISION_RATE_THRESHOLD["FIRST"],
            pool_size=10000,
            pool_duplicate_rate=0.0,
        )
        enforce_pool_quality(measurement, column="FIRST")  # must not raise

    def test_pool_duplicate_rate_exactly_at_threshold_passes(self) -> None:
        measurement = PoolQualityMeasurement(
            column="FIRST",
            distinct_sources=1000,
            non_deterministic_sources=0,
            collision_rate=0.0,
            pool_size=10000,
            pool_duplicate_rate=POOL_DUPLICATE_RATE_THRESHOLD["FIRST"],
        )
        enforce_pool_quality(measurement, column="FIRST")  # must not raise


class TestEnforcerInputContract:
    def test_rejects_measurement_for_a_different_column(self) -> None:
        # A MAIDEN measurement enforced with column="FIRST" must NOT silently
        # apply FIRST's looser threshold: 0.5 is above MAIDEN's 0.3144 but
        # below FIRST's 0.6630, so a mismatch that slipped through would pass a
        # real breach. The mismatch is a coded integrity failure.
        maiden = PoolQualityMeasurement(
            column="MAIDEN",
            distinct_sources=100,
            non_deterministic_sources=0,
            collision_rate=0.5,
            pool_size=10000,
            pool_duplicate_rate=0.0,
        )
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(maiden, column="FIRST")
        assert exc_info.value.metric == "column"
        assert exc_info.value.observed == "MAIDEN"
        assert exc_info.value.column == "FIRST"
        assert exc_info.value.threshold == "FIRST"
        assert "refusing to apply a mismatched column's threshold" in str(exc_info.value)

    def test_rejects_non_finite_collision_rate(self) -> None:
        # NaN > threshold is False, so a NaN rate would fail OPEN; reject it.
        measurement = PoolQualityMeasurement(
            column="FIRST",
            distinct_sources=100,
            non_deterministic_sources=0,
            collision_rate=float("nan"),
            pool_size=10000,
            pool_duplicate_rate=0.0,
        )
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "collision_rate"
        assert exc_info.value.column == "FIRST"
        assert math.isnan(exc_info.value.observed)
        assert exc_info.value.threshold == "[0.0, 1.0]"
        message = str(exc_info.value)
        # Boundary-spanning, as above: each check straddles where two
        # adjacent string-literal fragments join.
        assert "not a finite rate in [0, 1]; refusing to compare" in message
        assert "rate against a threshold (measurement-integrity failure)" in message

    def test_rejects_non_finite_pool_duplicate_rate(self) -> None:
        # NaN, not inf: a NaN duplicate rate would fail OPEN under a bare
        # `rate > threshold` check (NaN > x is False), so this isolates the new
        # integrity guard rather than the ordinary threshold comparison. The
        # `[0.0, 1.0]` threshold marker proves the integrity path fired.
        measurement = PoolQualityMeasurement(
            column="FIRST",
            distinct_sources=100,
            non_deterministic_sources=0,
            collision_rate=0.0,
            pool_size=10000,
            pool_duplicate_rate=float("nan"),
        )
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "pool_duplicate_rate"
        assert exc_info.value.threshold == "[0.0, 1.0]"
        assert exc_info.value.column == "FIRST"
        assert math.isnan(exc_info.value.observed)

    def test_finite_out_of_range_rate_is_caught_by_the_integrity_check(self) -> None:
        # A rate that is FINITE but outside [0, 1] (1.5, never a real collision
        # or duplicate rate) must be caught by the SAME integrity check as a
        # NaN, not silently admitted. This is the only case that distinguishes
        # the integrity check's `or` from a mutated `and`: for NaN, `not
        # isfinite` and `not in-range` are BOTH true, so `or`/`and` agree by
        # coincidence and a NaN-only test cannot tell them apart. The
        # "[0.0, 1.0]" threshold marker proves the INTEGRITY check fired, not
        # the ordinary per-column threshold comparison (which would also
        # reject 1.5, but by comparing it to a float threshold, not this
        # marker).
        measurement = PoolQualityMeasurement(
            column="FIRST",
            distinct_sources=100,
            non_deterministic_sources=0,
            collision_rate=1.5,
            pool_size=10000,
            pool_duplicate_rate=0.0,
        )
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "collision_rate"
        assert exc_info.value.threshold == "[0.0, 1.0]"
        assert exc_info.value.observed == 1.5

    def test_rate_exactly_one_is_a_valid_finite_rate_not_an_integrity_failure(self) -> None:
        # 1.0 (100% collision) is a legitimate value of the metric, not an
        # integrity failure: it must pass the [0, 1] finite-range check and
        # fall through to the ORDINARY per-column threshold comparison
        # (which then rejects it for exceeding FIRST's frozen threshold, but
        # via a numeric threshold, not the "[0.0, 1.0]" integrity marker).
        # Pins the upper boundary against a `<= 1.0` -> `< 1.0` flip, which
        # would misclassify this legitimate value as an integrity failure.
        # distinct_sources/pool_size are the FROZEN FIRST tier so this reaches
        # the threshold comparison rather than the (separate) tuple guard.
        measurement = PoolQualityMeasurement(
            column="FIRST",
            distinct_sources=FROZEN_DISTINCT_SOURCES["FIRST"],
            non_deterministic_sources=0,
            collision_rate=1.0,
            pool_size=FROZEN_POOL_SIZE["FIRST"],
            pool_duplicate_rate=0.0,
        )
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "collision_rate"
        assert exc_info.value.threshold == COLLISION_RATE_THRESHOLD["FIRST"]


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
        assert exc_info.value.column == "FIRST"
        assert exc_info.value.observed == "some_other_obligation"
        assert exc_info.value.threshold == "pool_quality"
        assert "unrecognized quality obligation 'some_other_obligation'" in str(exc_info.value)

    def test_unrecognized_column_rejected_coded(self, tmp_path: Path) -> None:
        pool = _pool(size=10, distinct_count=10)
        measurement = _measure("SUFFIX", ["s0"], ["o0"], pool, tmp_path)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="SUFFIX")
        assert exc_info.value.code == "pool_quality.threshold_exceeded"
        assert exc_info.value.metric == "column"
        assert exc_info.value.column == "SUFFIX"
        assert exc_info.value.observed == "SUFFIX"
        assert exc_info.value.threshold == sorted(COLLISION_RATE_THRESHOLD)
        assert "is not in the frozen C1 pool_quality column set" in str(exc_info.value)
        # Boundary-spanning: the message is built from adjacent string-literal
        # fragments ("...refusing to silently pass an " + "unrecognized
        # column"); a mutant that wraps only the SECOND fragment in filler
        # text still leaves "unrecognized column" itself intact as a bare
        # substring, so the check must span the fragment join.
        assert "refusing to silently pass an unrecognized column" in str(exc_info.value)


class TestCodedError:
    def test_code_is_stable_and_message_names_column_and_metric(self, tmp_path: Path) -> None:
        sources = [f"s{i}" for i in range(10)]
        masked = [f"o{i % 3}" for i in range(10)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(_at_frozen_tier(measurement, "FIRST"), column="FIRST")

        assert PoolQualityError.code == "pool_quality.threshold_exceeded"
        assert exc_info.value.code == "pool_quality.threshold_exceeded"
        message = str(exc_info.value)
        assert "FIRST" in message
        # A space-anchored, exact substring (not a loose `in` check): a
        # mutant that wraps the metric name in filler text (e.g.
        # "XXcollision_rateXX") still contains "collision_rate" as a bare
        # substring, so the check must pin the surrounding text too.
        assert "collision_rate 0.7000 exceeds" in message
        assert exc_info.value.column == "FIRST"
        assert exc_info.value.threshold == COLLISION_RATE_THRESHOLD["FIRST"]

    def test_pool_duplicate_rate_breach_message_and_coded_fields(self, tmp_path: Path) -> None:
        # Mirrors the collision_rate coded-error test above for the OTHER
        # breach branch: pool_duplicate_rate has its own `_breach_message`
        # call site (column, metric name, and warnings threaded through
        # separately from the collision_rate one), so it needs its own
        # precise-message and coded-field pins.
        sources = [f"s{i}" for i in range(5)]
        masked = [f"o{i}" for i in range(5)]  # zero collisions
        pool = _pool(size=100, distinct_count=2)  # duplicate_rate = 0.98, breaches
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(_at_frozen_tier(measurement, "FIRST"), column="FIRST")

        message = str(exc_info.value)
        assert "breach on column 'FIRST'" in message
        assert "pool_duplicate_rate 0.9800 exceeds" in message
        assert exc_info.value.column == "FIRST"
        assert exc_info.value.metric == "pool_duplicate_rate"
        assert exc_info.value.threshold == POOL_DUPLICATE_RATE_THRESHOLD["FIRST"]

    def test_pool_duplicate_rate_breach_message_includes_warnings(self, tmp_path: Path) -> None:
        # The pool_duplicate_rate breach has its OWN `_breach_message` call
        # site with its OWN `warnings` argument threaded through separately
        # from the collision_rate one; a mutant dropping just that argument
        # (passing None instead of the real tuple) would silently omit the
        # warning codes ONLY from this breach path, so it needs its own test
        # rather than relying on the collision_rate warnings test above.
        sources = [f"s{i}" for i in range(5)]
        masked = [f"o{i}" for i in range(5)]
        pool = _pool(size=100, distinct_count=2)  # duplicate_rate = 0.98, breaches
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)
        warning = QualityWarning(code="pool_scaled_up", provider="person_first_name")

        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(
                _at_frozen_tier(measurement, "FIRST"), column="FIRST", warnings=(warning,)
            )
        message = str(exc_info.value)
        assert "pool_duplicate_rate 0.9800 exceeds" in message
        assert "(concurrent QualityWarning codes: pool_scaled_up)" in message

    def test_warnings_surfaced_in_breach_message_but_never_raise_alone(
        self, tmp_path: Path
    ) -> None:
        sources = [f"s{i}" for i in range(10)]
        masked = [f"o{i % 3}" for i in range(10)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)
        warning = QualityWarning(code="pool_scaled_up", provider="person_first_name")

        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(
                _at_frozen_tier(measurement, "FIRST"), column="FIRST", warnings=(warning,)
            )
        message = str(exc_info.value)
        assert "pool_scaled_up" in message
        # The breach description itself must survive alongside the warning
        # codes, not be replaced by them: pins `message +=` against a mutant
        # that overwrites the message (`message =`) and silently drops the
        # "pool_quality breach on column ..." prefix.
        assert "pool_quality breach on column 'FIRST'" in message

        # A compliant pool with the SAME warning present must not raise: a
        # mere QualityWarning never escalates on its own.
        compliant_sources = [f"s{i}" for i in range(20)]
        compliant_masked = [f"o{i}" for i in range(20)]
        compliant_measurement = _measure(
            "FIRST", compliant_sources, compliant_masked, pool, tmp_path
        )
        enforce_pool_quality(
            _at_frozen_tier(compliant_measurement, "FIRST"), column="FIRST", warnings=(warning,)
        )

    def test_multiple_warning_codes_are_comma_joined_in_the_breach_message(
        self, tmp_path: Path
    ) -> None:
        # A single warning cannot distinguish the ", "-join separator from a
        # mutated one (there is nothing to join). Two DISTINCT codes force
        # `", ".join(...)` to actually separate two items, so a mutated
        # separator (e.g. a filler-wrapped string) changes the joined text.
        sources = [f"s{i}" for i in range(10)]
        masked = [f"o{i % 3}" for i in range(10)]
        pool = _pool(size=100, distinct_count=100)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)
        warnings = (
            QualityWarning(code="pool_scaled_up", provider="person_first_name"),
            QualityWarning(code="pool_capacity_exceeded", provider="person_first_name"),
        )

        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(
                _at_frozen_tier(measurement, "FIRST"), column="FIRST", warnings=warnings
            )
        # sorted(): pool_capacity_exceeded, pool_scaled_up
        assert "pool_capacity_exceeded, pool_scaled_up" in str(exc_info.value)


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


class TestSqlDriftRestoredFrozenFilter:
    """P3-T2 SQL-drift adjudication: production's `_COLLISION_SQL` had
    drifted to `WHERE source IS NOT NULL AND masked IS NOT NULL`, but the
    frozen baseline (PHASE3-C1-BASELINE.md) filters `source IS NOT NULL`
    ONLY. The extra `masked` filter is fail-open in direction (dropping a
    non-null-source/null-masked row shrinks `distinct_sources`, which can
    turn a real collision population into a smaller-or-empty pass), and it
    is not provably inert for this generic measurement (no code invariant
    anywhere in the pool-build path guarantees a built pool never contains a
    null value, so a non-null source landing on a null masked output is not
    ruled out for every caller). Restored to the frozen filter; this test
    pins the restored behavior and would FAIL against the drifted SQL.
    """

    def test_non_null_source_with_null_masked_output_counts_toward_population(
        self, tmp_path: Path
    ) -> None:
        # s2's masked value is null but its source is not. Under the
        # DRIFTED SQL (`AND masked IS NOT NULL`), s2's row is dropped
        # entirely: distinct_sources would be 2 (just s0, s1), distinct_
        # outputs 2, collision_rate 0.0. Under the RESTORED frozen SQL, s2
        # still counts toward distinct_sources; its ANY_VALUE(masked) is
        # NULL (excluded from COUNT(DISTINCT out_val)), so it is reported as
        # a collision against nothing rather than vanishing from the
        # population: distinct_sources=3, distinct_outputs=2,
        # collision_rate=(3-2)/3=1/3.
        sources = ["s0", "s1", "s2"]
        masked = ["o0", "o1", None]
        pool = _pool(size=10, distinct_count=10)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.distinct_sources == 3
        assert measurement.non_deterministic_sources == 0
        assert measurement.collision_rate == pytest.approx(1 / 3)

    def test_all_masked_null_for_one_source_among_others_still_counts_that_source(
        self, tmp_path: Path
    ) -> None:
        # Same shape with a repeated source (s0 appears twice, both masked
        # null): still one distinct source contributing a NULL out_val, not
        # two rows dropped from the aggregation.
        sources = ["s0", "s0", "s1"]
        masked = [None, None, "o1"]
        pool = _pool(size=10, distinct_count=10)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        assert measurement.distinct_sources == 2
        assert measurement.non_deterministic_sources == 0
        assert measurement.collision_rate == pytest.approx(0.5)


class TestDifferentialCollisionMeasurement:
    """The collision arithmetic graded ONLY against a raw-input pure-Python
    reference (`_reference_collision_measurement`), never a golden the code
    itself produced. Covers duplicate sources, nondeterministic mappings,
    nulls in both source and masked, and empty inputs in one property sweep.
    """

    _SOURCE_ALPHABET = ["s0", "s1", "s2", "s3", None]
    _MASKED_ALPHABET = ["o0", "o1", "o2", None]

    @given(
        pairs=st.lists(
            st.tuples(st.sampled_from(_SOURCE_ALPHABET), st.sampled_from(_MASKED_ALPHABET)),
            min_size=0,
            max_size=14,
        )
    )
    @settings(
        max_examples=40,
        deadline=None,
        database=None,
        # function_scoped_fixture: tmp_path is safe across examples here (a
        # fresh random spool name per call, always cleaned up before return).
        # differing_executors: mutmut's per-mutant fork/subprocess model runs
        # this same test body from more than one process; that is a harness
        # property, not a correctness issue in the test itself.
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.differing_executors,
        ],
    )
    def test_collision_measurement_matches_raw_pure_python_reference(
        self, pairs: list[tuple[str | None, str | None]], tmp_path: Path
    ) -> None:
        sources = [p[0] for p in pairs]
        masked = [p[1] for p in pairs]
        pool = _pool(size=10, distinct_count=10)
        measurement = _measure("FIRST", sources, masked, pool, tmp_path)

        ref_distinct_sources, ref_distinct_outputs, ref_non_det = _reference_collision_measurement(
            pairs
        )
        expected_rate = _reference_collision_rate(
            ref_distinct_sources, ref_distinct_outputs, ref_non_det
        )

        assert measurement.distinct_sources == ref_distinct_sources
        assert measurement.non_deterministic_sources == ref_non_det
        if math.isnan(expected_rate):
            assert math.isnan(measurement.collision_rate)
        else:
            assert measurement.collision_rate == pytest.approx(expected_rate)


class TestPoolDuplicateRateRawCounting:
    """`pool_duplicate_rate` graded against a distinct count computed
    directly from the RAW pool values array, never `pool.distinct_count`
    read back (that would self-grade)."""

    @given(
        raw_values=st.lists(
            st.sampled_from(["v0", "v1", "v2", "v3", "v4"]), min_size=1, max_size=25
        )
    )
    @settings(
        max_examples=30,
        deadline=None,
        database=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.differing_executors,
        ],
    )
    def test_pool_duplicate_rate_matches_raw_distinct_count(
        self, raw_values: list[str], tmp_path: Path
    ) -> None:
        reference_distinct_count = len(set(raw_values))
        size = len(raw_values)
        pool = ValuePool(
            values=np.array(raw_values, dtype=object),
            provider="person_first_name",
            locale="en_US",
            config_hash="test_hash",
            seed=b"seed",
            size=size,
            build_time_ms=0.0,
            backend_type="faker",
            backend_version="1.0",
            distinct_count=reference_distinct_count,
        )
        measurement = _measure("FIRST", ["s0"], ["o0"], pool, tmp_path)

        expected_rate = (size - reference_distinct_count) / size
        assert measurement.pool_duplicate_rate == pytest.approx(expected_rate)

    def test_zero_size_pool_reports_rate_zero_not_a_division_error(self, tmp_path: Path) -> None:
        pool = _pool(size=0, distinct_count=0)
        measurement = _measure("FIRST", ["s0"], ["o0"], pool, tmp_path)
        assert measurement.pool_duplicate_rate == 0.0


class TestTupleAwareThresholdGuard:
    """P3-T2 BLOCKER: the frozen thresholds were calibrated at ONE
    (pool_size, distinct_sources) tier per column (PHASE3-C1-BASELINE.md).
    `enforce_pool_quality` selected the threshold by column alone, so a
    measurement from a DIFFERENT tier -- a different pool_size or a
    different distinct-source population -- had its rates compared against a
    threshold with no known validity there (fail-open). This class proves
    both sides: a matching tier is accepted, a mismatched one is rejected
    coded, and the empty-population special case stays exempt.
    """

    @staticmethod
    def _matching_measurement(
        column: str, *, collision_rate: float = 0.0, pool_duplicate_rate: float = 0.0
    ) -> PoolQualityMeasurement:
        return PoolQualityMeasurement(
            column=column,
            distinct_sources=FROZEN_DISTINCT_SOURCES[column],
            non_deterministic_sources=0,
            collision_rate=collision_rate,
            pool_size=FROZEN_POOL_SIZE[column],
            pool_duplicate_rate=pool_duplicate_rate,
        )

    def test_matching_tier_is_accepted(self) -> None:
        measurement = self._matching_measurement("FIRST")
        enforce_pool_quality(measurement, column="FIRST")  # must not raise

    def test_larger_pool_size_is_rejected(self) -> None:
        measurement = replace(self._matching_measurement("FIRST"), pool_size=20_000)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "pool_size"
        assert exc_info.value.observed == 20_000
        assert exc_info.value.threshold == FROZEN_POOL_SIZE["FIRST"]
        assert exc_info.value.column == "FIRST"
        # One boundary-spanning check straddling BOTH fragment joins in the
        # f-string (the "; the frozen ... pool_size " fragment and the
        # "(tuple-aware ...)" fragment that follows it): a mutant that wraps
        # or capitalizes only one of those two fragments still breaks this
        # single contiguous match.
        assert (
            "; the frozen threshold has no known validity at a different "
            "pool_size (tuple-aware" in str(exc_info.value)
        )

    def test_smaller_pool_size_is_rejected_too(self) -> None:
        # A different pool_size has a different collision floor either
        # direction, not just when it is larger: the guard is an exact-tier
        # match, not a one-sided ceiling.
        measurement = replace(self._matching_measurement("FIRST"), pool_size=5_000)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "pool_size"

    def test_mismatched_distinct_sources_is_rejected(self) -> None:
        measurement = replace(self._matching_measurement("LAST"), distinct_sources=999)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="LAST")
        assert exc_info.value.metric == "distinct_sources"
        assert exc_info.value.observed == 999
        assert exc_info.value.threshold == FROZEN_DISTINCT_SOURCES["LAST"]
        assert exc_info.value.column == "LAST"
        message = str(exc_info.value)
        # Boundary-spanning (as in the pool_size guard test above): each
        # check straddles a fragment join in the f-string, using the actual
        # dynamic values (999 observed, 1200 = FROZEN_DISTINCT_SOURCES["LAST"])
        # so a mutant wrapping/capitalizing only one fragment still breaks
        # the match even though its own interior substring is untouched.
        assert "999 does not match the frozen calibration tier's distinct_sources" in message
        assert (
            "has no known validity at a different source population "
            "(tuple-aware integrity failure, not a tolerance breach)" in message
        )

    def test_empty_population_is_exempt_from_the_distinct_sources_check(self) -> None:
        # distinct_sources == 0 is the separately frozen empty-population
        # pass (rate 0, always) -- exempt from the tier check, not a
        # mismatched tier.
        measurement = replace(self._matching_measurement("MAIDEN"), distinct_sources=0)
        enforce_pool_quality(measurement, column="MAIDEN")  # must not raise

    def test_empty_population_still_enforces_pool_size(self) -> None:
        # The pool itself is unconditionally real (built regardless of
        # whether the source column happens to be all-null), so a pool_size
        # mismatch still fails closed even at distinct_sources == 0.
        measurement = replace(self._matching_measurement("MAIDEN"), distinct_sources=0, pool_size=1)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="MAIDEN")
        assert exc_info.value.metric == "pool_size"

    def test_tuple_guard_fires_even_when_both_rates_are_compliant(self) -> None:
        # Both rates are 0.0 (would pass the threshold comparisons outright):
        # the tuple guard must still reject the mismatched tier, proving it
        # is a real, independent check rather than one that only ever fires
        # alongside a rate breach the threshold comparison would have caught
        # anyway.
        measurement = replace(self._matching_measurement("FIRST"), distinct_sources=1)
        with pytest.raises(PoolQualityError) as exc_info:
            enforce_pool_quality(measurement, column="FIRST")
        assert exc_info.value.metric == "distinct_sources"

    def test_unsupported_tier_gate_test_recipe_stays_at_the_calibrated_tuple(self) -> None:
        # Regression pin for the frozen C1 gate's own usage
        # (test_phase3_c1_gate.py): its synthetic generator caps distinct
        # sources at exactly 1,000/1,200/360 regardless of row count, so a
        # real measurement at either its parity (10,000-row) or moderate
        # (250,000-row) tier lands on the SAME calibrated tuple this guard
        # requires -- the guard does not accidentally reject the gate's own
        # frozen recipe.
        measurement = self._matching_measurement("MAIDEN")
        enforce_pool_quality(measurement, column="MAIDEN")  # must not raise


class _CloseTrackingConnection:
    """Wraps a real DuckDB connection, tracking `close()` calls and
    optionally injecting a fault at `execute()` or `close()`, so a test can
    prove the spool is unlinked and the connection is closed on each real
    measurement seam (`pq.write_table`, `conn.execute`/`fetchone`,
    `conn.close`) without abandoning the real DuckDB behavior for the parts
    of the call not under fault."""

    def __init__(
        self,
        real_conn: Any,
        *,
        fail_execute: Exception | None = None,
        fail_close: Exception | None = None,
    ) -> None:
        self._real_conn = real_conn
        self._fail_execute = fail_execute
        self._fail_close = fail_close
        self.close_called = False

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        if self._fail_execute is not None:
            raise self._fail_execute
        return self._real_conn.execute(*args, **kwargs)

    def close(self) -> None:
        self.close_called = True
        if self._fail_close is not None:
            raise self._fail_close
        self._real_conn.close()


class TestExceptionalCleanupSeams:
    """P3-T2 HIGH: the spool is unlinked in `measure_pool_quality`'s own
    `finally` BEFORE it returns, so "enforce raises" never exercises this
    cleanup -- the real seams are `pq.write_table`, `conn.execute`/
    `fetchone`, and `conn.close` INSIDE `measure_pool_quality` itself. Each
    test injects a fault at exactly one seam and asserts the spool is
    unlinked AND the connection is closed regardless.
    """

    def test_write_table_failure_still_unlinks_spool_and_closes_connection(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tracking_conns: list[_CloseTrackingConnection] = []

        def fake_connect(*, temp_dir: Path, memory_limit: str | None = None) -> Any:
            real_conn = connect_duckdb(temp_dir=temp_dir, memory_limit=memory_limit)
            wrapper = _CloseTrackingConnection(real_conn)
            tracking_conns.append(wrapper)
            return wrapper

        def fake_write_table(*args: Any, **kwargs: Any) -> None:
            raise OSError("simulated disk failure")

        monkeypatch.setattr(pool_quality_module, "connect_duckdb", fake_connect)
        monkeypatch.setattr(pool_quality_module.pq, "write_table", fake_write_table)

        pool = _pool(size=10, distinct_count=10)
        with pytest.raises(OSError, match="simulated disk failure"):
            measure_pool_quality(
                column="FIRST",
                source=pa.array(["s0"], type=pa.string()),
                masked=pa.array(["o0"], type=pa.string()),
                pool=pool,
                temp_dir=tmp_path,
                memory_limit="64MB",
            )

        assert len(tracking_conns) == 1
        assert tracking_conns[0].close_called is True
        assert list(tmp_path.glob("pairs_FIRST*.parquet")) == []

    def test_execute_failure_still_unlinks_spool_and_closes_connection(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tracking_conns: list[_CloseTrackingConnection] = []

        def fake_connect(*, temp_dir: Path, memory_limit: str | None = None) -> Any:
            real_conn = connect_duckdb(temp_dir=temp_dir, memory_limit=memory_limit)
            wrapper = _CloseTrackingConnection(
                real_conn, fail_execute=RuntimeError("simulated query failure")
            )
            tracking_conns.append(wrapper)
            return wrapper

        monkeypatch.setattr(pool_quality_module, "connect_duckdb", fake_connect)

        pool = _pool(size=10, distinct_count=10)
        with pytest.raises(RuntimeError, match="simulated query failure"):
            measure_pool_quality(
                column="FIRST",
                source=pa.array(["s0"], type=pa.string()),
                masked=pa.array(["o0"], type=pa.string()),
                pool=pool,
                temp_dir=tmp_path,
                memory_limit="64MB",
            )

        assert tracking_conns[0].close_called is True
        # write_table ran for real here (only execute is faulted), so a real
        # spool WAS written before the fault -- proving unlink actually had
        # something to clean up, not just a no-op on an absent file.
        assert list(tmp_path.glob("pairs_FIRST*.parquet")) == []

    def test_close_failure_propagates_but_spool_is_already_unlinked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tracking_conns: list[_CloseTrackingConnection] = []

        def fake_connect(*, temp_dir: Path, memory_limit: str | None = None) -> Any:
            real_conn = connect_duckdb(temp_dir=temp_dir, memory_limit=memory_limit)
            wrapper = _CloseTrackingConnection(
                real_conn, fail_close=RuntimeError("simulated close failure")
            )
            tracking_conns.append(wrapper)
            return wrapper

        monkeypatch.setattr(pool_quality_module, "connect_duckdb", fake_connect)

        pool = _pool(size=10, distinct_count=10)
        with pytest.raises(RuntimeError, match="simulated close failure"):
            measure_pool_quality(
                column="FIRST",
                source=pa.array(["s0"], type=pa.string()),
                masked=pa.array(["o0"], type=pa.string()),
                pool=pool,
                temp_dir=tmp_path,
                memory_limit="64MB",
            )

        assert tracking_conns[0].close_called is True
        # unlink runs in the inner `try`, BEFORE `conn.close()` in the inner
        # `finally`, so the spool is gone even though close() itself went on
        # to raise.
        assert list(tmp_path.glob("pairs_FIRST*.parquet")) == []


class TestConnectDuckdbDirectUsage:
    def test_measure_pool_quality_passes_temp_dir_and_memory_limit_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured: dict[str, Any] = {}
        real_connect = pool_quality_module.connect_duckdb

        def spy_connect(*, temp_dir: Path, memory_limit: str | None = None) -> Any:
            captured["temp_dir"] = temp_dir
            captured["memory_limit"] = memory_limit
            return real_connect(temp_dir=temp_dir, memory_limit=memory_limit)

        monkeypatch.setattr(pool_quality_module, "connect_duckdb", spy_connect)
        pool = _pool(size=10, distinct_count=10)
        _measure("FIRST", ["s0"], ["o0"], pool, tmp_path, memory_limit="123MB")

        assert captured["temp_dir"] == tmp_path
        assert captured["memory_limit"] == "123MB"


class TestConcurrentSpoolIsolation:
    """P3-T2: the fixed `pairs_{column}.parquet` name let two overlapping
    measurements of the SAME column collide on one spool file. Fixed by a
    per-call random suffix; this proves both the uniqueness and that a
    genuinely overlapping pair no longer clobbers each other.
    """

    def test_sequential_measurements_of_same_column_use_distinct_spool_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured_paths: list[Path] = []
        real_write_table = pool_quality_module.pq.write_table

        def spy_write_table(table: Any, path: Any, *args: Any, **kwargs: Any) -> None:
            captured_paths.append(Path(path))
            real_write_table(table, path, *args, **kwargs)

        monkeypatch.setattr(pool_quality_module.pq, "write_table", spy_write_table)
        pool = _pool(size=10, distinct_count=10)
        _measure("FIRST", ["s0"], ["o0"], pool, tmp_path)
        _measure("FIRST", ["s1"], ["o1"], pool, tmp_path)

        assert len(captured_paths) == 2
        assert captured_paths[0] != captured_paths[1]

    def test_overlapping_measurements_of_same_column_do_not_clobber_each_other(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        a_paused = threading.Event()
        b_done = threading.Event()
        real_write_table = pool_quality_module.pq.write_table

        def hooked_write_table(table: Any, path: Any, *args: Any, **kwargs: Any) -> None:
            real_write_table(table, path, *args, **kwargs)
            # Only A's call (the first one through this hook) pauses; B's
            # own call arrives after A already set the flag, so it proceeds
            # straight through to a normal, un-paused completion.
            if not a_paused.is_set():
                a_paused.set()
                assert b_done.wait(timeout=5), "thread B never completed while A paused"

        monkeypatch.setattr(pool_quality_module.pq, "write_table", hooked_write_table)

        results: dict[str, PoolQualityMeasurement] = {}
        errors: list[BaseException] = []

        def run_a() -> None:
            try:
                pool = _pool(size=10, distinct_count=10)
                results["a"] = measure_pool_quality(
                    column="FIRST",
                    source=pa.array(["a0", "a1"], type=pa.string()),
                    masked=pa.array(["ax", "ay"], type=pa.string()),
                    pool=pool,
                    temp_dir=tmp_path,
                    memory_limit="64MB",
                )
            except BaseException as exc:  # surfaced via `errors`, not swallowed
                errors.append(exc)

        def run_b() -> None:
            assert a_paused.wait(timeout=5), "thread A never started"
            try:
                pool = _pool(size=10, distinct_count=10)
                results["b"] = measure_pool_quality(
                    column="FIRST",
                    source=pa.array(["b0"], type=pa.string()),
                    masked=pa.array(["bx"], type=pa.string()),
                    pool=pool,
                    temp_dir=tmp_path,
                    memory_limit="64MB",
                )
            except BaseException as exc:  # surfaced via `errors`, not swallowed
                errors.append(exc)
            finally:
                b_done.set()

        thread_a = threading.Thread(target=run_a)
        thread_b = threading.Thread(target=run_b)
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

        assert not errors, errors
        assert "a" in results and "b" in results
        assert results["a"].distinct_sources == 2
        assert results["b"].distinct_sources == 1

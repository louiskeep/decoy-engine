"""Task 3.4: chunk diagnostic-aggregation contract for the native C1 route.

Covers `execution/native/_route_diagnostics.py`'s `RouteDiagnostics`: isolation
from a shared `PoolCache`'s prior-invocation warnings, dedup + deterministic
ordering (mirroring `execution/_chunked.py`'s `aggregate_chunk_warnings`),
attribution by pool/table/column, and the row-error surfacing contract.
`test_c1_bounded_state.py` covers the adversary (state stays bounded); this
file covers correctness of what gets collected.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._chunked import aggregate_chunk_warnings
from decoy_engine.execution._row_errors import RowError, RowErrorRecord
from decoy_engine.execution.native._dispatch import (
    NativeRouteEvidence,
    run_native_or_oracle_chunked,
)
from decoy_engine.execution.native._route_diagnostics import (
    AttributedWarning,
    PoolOwner,
    RouteDiagnostics,
)
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._value_pool import ValuePool
from decoy_engine.keyprovider import SecretKeyProvider

_ENGINE_VERSION = "phase3-task3.4"


def _pool(provider: str, *, size: int, seed: bytes, locale: str = "default") -> ValuePool:
    """A synthetic pool sized precisely enough to drive `PoolCache`'s
    dominate-threshold math deterministically (8 bytes/value, float64), so
    these tests never depend on real Faker output lengths."""
    return ValuePool(
        values=np.arange(size, dtype=np.float64),
        provider=provider,
        locale=locale,
        config_hash="cfg0",
        seed=seed,
        size=size,
        build_time_ms=0.0,
        backend_type="test",
        backend_version="0",
        distinct_count=size,
    )


# ---------------------------------------------------------------------------
# Isolation: a shared cache's prior-invocation warnings never leak in.
# ---------------------------------------------------------------------------


def test_isolation_excludes_prior_invocation_warnings() -> None:
    cache = PoolCache(max_bytes=1000)
    # A prior invocation already left a warning in this shared cache.
    cache.put(_pool("prior_provider", size=40, seed=b"priorseed"))  # 320 bytes > 250 threshold
    assert len(cache.warnings()) == 1

    diag = RouteDiagnostics(cache)
    cache.put(_pool("this_invocation", size=40, seed=b"thisseed1"))

    seen_providers = [w.warning.provider for w in diag.pool_warnings()]
    assert seen_providers == ["this_invocation"]
    # The shared cache itself still carries both -- isolation is the
    # collector's view, not a mutation of the cache's own accumulation.
    assert len(cache.warnings()) == 2


def test_diagnostics_constructed_before_any_prior_warning_sees_everything_after() -> None:
    # A collector only excludes what predates its OWN construction, not
    # everything ever put -- constructing it first and then observing two
    # builds should report both.
    cache = PoolCache(max_bytes=1000)
    diag = RouteDiagnostics(cache)
    cache.put(_pool("a", size=40, seed=b"seed0001"))
    cache.put(_pool("b", size=40, seed=b"seed0002"))

    assert [w.warning.provider for w in diag.pool_warnings()] == ["a", "b"]


# ---------------------------------------------------------------------------
# Dedup + deterministic ordering, mirroring the oracle's aggregate_chunk_warnings.
# ---------------------------------------------------------------------------


def test_repeated_identical_warning_dedupes_to_one_first_emission_order() -> None:
    cache = PoolCache(max_bytes=1000)
    diag = RouteDiagnostics(cache)

    cache.put(_pool("p1", size=40, seed=b"seed0001"))  # emits W(p1)
    cache.put(_pool("p2", size=40, seed=b"seed0002"))  # emits W(p2); no eviction (640/1000)
    # A different pool identity (distinct seed) for the SAME provider at the
    # SAME size produces a QualityWarning equal in every field to the first
    # (QualityWarning carries no identity, only provider + detail) -- exactly
    # the "same warning re-emitted" case `aggregate_chunk_warnings` collapses.
    cache.put(_pool("p1", size=40, seed=b"seed0003"))  # emits W(p1) again, equal to the first

    warnings = diag.pool_warnings()
    providers = [w.warning.provider for w in warnings]
    assert providers == ["p1", "p2"]  # first-emission order; the repeat collapses in place


def test_parity_with_oracle_aggregate_chunk_warnings() -> None:
    """Feeding the SAME raw warning stream (with a duplicate) through the
    oracle's own per-chunk aggregator and through this collector's `_dedup_
    ordered` must produce the exact same deduped, ordered tuple -- proving the
    two dedup/ordering implementations agree. Built as a raw list directly:
    PoolCache now dedups identical emissions at the source (a re-put of the same
    dominating identity), so the duplicate cannot be produced through the cache."""
    from decoy_engine.execution.native._route_diagnostics import _dedup_ordered
    from decoy_engine.generation.pool._events import QualityWarning

    w_p1 = QualityWarning(code="pool_dominates_cache", provider="p1", detail={"pool_bytes": 320})
    w_p2 = QualityWarning(code="pool_dominates_cache", provider="p2", detail={"pool_bytes": 320})
    w_p1_dup = QualityWarning(code="pool_dominates_cache", provider="p1", detail={"pool_bytes": 320})
    assert w_p1 == w_p1_dup  # identical -> both dedup paths must collapse them
    raw = (w_p1, w_p2, w_p1_dup)

    class _FakeChunkResult:
        def __init__(self, warnings: tuple) -> None:
            self.warnings = warnings

    # The repeat lands in a later chunk than its first occurrence -- the case
    # the oracle aggregator's own docstring names explicitly.
    oracle_aggregated = aggregate_chunk_warnings(
        [_FakeChunkResult((raw[0],)), _FakeChunkResult((raw[1], raw[2]))]
    )
    collector_deduped = _dedup_ordered(raw)

    assert collector_deduped == oracle_aggregated == (w_p1, w_p2)


def test_same_input_same_order_deterministic() -> None:
    def _drive() -> tuple[str, ...]:
        cache = PoolCache(max_bytes=1000)
        diag = RouteDiagnostics(cache)
        cache.put(_pool("a", size=40, seed=b"seed0001"))
        cache.put(_pool("c", size=40, seed=b"seed0002"))
        cache.put(_pool("b", size=40, seed=b"seed0003"))
        return tuple(w.warning.provider for w in diag.pool_warnings())

    assert _drive() == _drive() == ("a", "c", "b")


# ---------------------------------------------------------------------------
# Attribution by pool/table/column, across multiple tables.
# ---------------------------------------------------------------------------


def test_attribution_fans_out_across_columns_sharing_one_provider() -> None:
    # C1's LAST and MAIDEN both use person_last_name under distinct
    # namespaces (PHASE3-C1-BASELINE.md): QualityWarning carries only
    # provider, so a warning on a shared provider cannot be disambiguated
    # further -- every registered owner for that provider is attached.
    cache = PoolCache(max_bytes=1000)
    diag = RouteDiagnostics(cache)
    cache.put(_pool("person_last_name", size=40, seed=b"lastseed1"))

    owners = [
        PoolOwner(table="patients", column="LAST", provider="person_last_name"),
        PoolOwner(table="patients", column="MAIDEN", provider="person_last_name"),
    ]
    warnings = diag.pool_warnings(owners=owners)

    assert len(warnings) == 1
    assert isinstance(warnings[0], AttributedWarning)
    assert {(o.table, o.column) for o in warnings[0].owners} == {
        ("patients", "LAST"),
        ("patients", "MAIDEN"),
    }


def test_multi_table_attribution_and_cross_invocation_isolation() -> None:
    """Two invocation-scoped collectors sharing one cache: each must
    attribute only its own table's provider and never see the other's
    warnings -- both halves of the Task 3.4 contract in one drive."""
    cache = PoolCache(max_bytes=1000)

    diag_patients = RouteDiagnostics(cache)
    cache.put(_pool("person_first_name", size=40, seed=b"firstseed"))
    cache.put(_pool("person_last_name", size=40, seed=b"lastseed2"))
    # Read immediately after patients' own builds -- mirroring the real
    # coordinator, which constructs one RouteDiagnostics per table right
    # before dispatching it and reads it right after that table's chunk
    # stream drains, before the next table's invocation starts.
    patients_warnings = diag_patients.pool_warnings(
        owners=[
            PoolOwner("patients", "FIRST", "person_first_name"),
            PoolOwner("patients", "LAST", "person_last_name"),
        ]
    )
    assert {w.warning.provider for w in patients_warnings} == {
        "person_first_name",
        "person_last_name",
    }

    # A second table's invocation starts AFTER patients' builds landed; its
    # baseline excludes them.
    diag_observations = RouteDiagnostics(cache)
    cache.put(_pool("observation_code", size=40, seed=b"obsseed01"))

    observations_warnings = diag_observations.pool_warnings(
        owners=[PoolOwner("observations", "CODE", "observation_code")]
    )
    assert [w.warning.provider for w in observations_warnings] == ["observation_code"]


# ---------------------------------------------------------------------------
# Row errors: no native producer today, but the contract must not drop one.
# ---------------------------------------------------------------------------


def test_row_error_in_a_later_chunk_captured_and_surfaced_in_job_evidence() -> None:
    cache = PoolCache(max_bytes=10**9)
    diag = RouteDiagnostics(cache)

    # Chunks 1-2 mask cleanly (nothing recorded); chunk 3 hits a row error.
    diag.record_row_error(
        RowError(column="FIRST", row_index=42, trigger="mask_error", reason="boom"),
        table="patients",
    )

    evidence = diag.evidence()
    assert evidence.row_errors == (
        RowErrorRecord(
            table="patients", column="FIRST", row_index=42, trigger="mask_error", reason="boom"
        ),
    )

    with pytest.raises(RowErrorsFailedError) as exc_info:
        diag.raise_if_row_errors()
    assert exc_info.value.records == evidence.row_errors


def test_repeated_identical_row_error_dedupes_to_one() -> None:
    cache = PoolCache(max_bytes=10**9)
    diag = RouteDiagnostics(cache)
    err = RowError(column="X", row_index=1, trigger="format_error", reason="bad cell")

    diag.record_row_error(err, table="t")
    diag.record_row_error(err, table="t")

    assert len(diag.row_errors()) == 1


def test_no_row_errors_never_raises() -> None:
    cache = PoolCache(max_bytes=10**9)
    diag = RouteDiagnostics(cache)
    diag.raise_if_row_errors()  # must not raise
    assert diag.evidence().row_errors == ()


# ---------------------------------------------------------------------------
# End-to-end: the real native dispatch route, sharing a pre-populated cache.
# ---------------------------------------------------------------------------


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=bytes(range(32)), key_version="v1")


def _faker_config(pool_size: int = 50) -> dict:
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260830, "post_validation": False},
        "sources": {"t": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "targets": {"t": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "FIRST",
                        "strategy": "faker",
                        "provider": "person_first_name",
                        "deterministic": True,
                        "namespace": "first_name_identity",
                        "pool_size": pool_size,
                    }
                ],
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _source(n: int = 20, n_distinct: int = 8) -> pa.Table:
    values = [f"src_{i % n_distinct}" if i % 7 != 0 else None for i in range(n)]
    return pa.table({"FIRST": pa.array(values, type=pa.string())})


def _chunk(table: pa.Table, batch_size: int) -> list[pa.Table]:
    return [table.slice(i, batch_size) for i in range(0, table.num_rows, batch_size)]


def _run(config: dict, source: pa.Table, batch_size: int, cache: PoolCache) -> NativeRouteEvidence:
    sink: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, batch_size),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(),
            route_evidence_sink=sink,
            pool_cache=cache,
        )
    )
    return sink[0]


def test_real_dispatch_isolates_pool_warning_from_a_prior_invocation() -> None:
    config = _faker_config(pool_size=50)
    source = _source()

    # Measurement pass: build the SAME identity pool this config always
    # builds (fixed seed 20260830) under a generous cache, so the exact
    # pool_bytes this run produces is known -- no guessing at real Faker
    # string lengths.
    measuring_cache = PoolCache(max_bytes=256 * 1024 * 1024)
    evidence = _run(config, source, 5, measuring_cache)
    assert evidence.native_admitted is True
    measured_bytes = measuring_cache.stats().bytes_used
    assert measured_bytes > 0

    # Size a cache so this pool alone exceeds the 25% dominate threshold
    # (3x the measured size: dominate_threshold = 0.75x measured > measured
    # is false, but max_bytes = 3x measured > measured avoids a capacity
    # error while 0.25 * 3x = 0.75x measured < measured triggers dominate).
    cache = PoolCache(max_bytes=measured_bytes * 3)
    # A prior invocation already left an unrelated warning here: sized to
    # clear the new cache's own 25% dominate threshold (0.75x measured_bytes)
    # with margin, while staying well under its 3x capacity.
    prior_size = int(measured_bytes * 0.75 / 8) + 100
    cache.put(_pool("prior_provider", size=prior_size, seed=b"priorseed"))
    assert len(cache.warnings()) == 1

    diag = RouteDiagnostics(cache)
    evidence = _run(config, source, 5, cache)
    assert evidence.native_admitted is True

    new_warnings = diag.pool_warnings(
        owners=[PoolOwner(table="t", column="FIRST", provider="person_first_name")]
    )
    assert len(new_warnings) == 1
    assert new_warnings[0].warning.code == "pool_dominates_cache"
    assert new_warnings[0].warning.provider == "person_first_name"
    assert {(o.table, o.column) for o in new_warnings[0].owners} == {("t", "FIRST")}
    # The prior invocation's warning is excluded from this invocation's view,
    # even though the shared cache still carries it.
    assert all(w.warning.provider != "prior_provider" for w in new_warnings)
    assert len(cache.warnings()) == 2

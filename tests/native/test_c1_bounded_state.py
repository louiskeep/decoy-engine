"""Task 3.4: bounded-state adversary for the native C1 route.

Drives a high-cardinality deterministic C1 faker input through
`run_native_or_oracle_chunked` and asserts the state owners this route (and
Task 3.4's own `RouteDiagnostics`) touch stay bounded: `PoolCache` never
grows past its byte bound, and the diagnostic collector's size tracks column
count, not chunk count or distinct-source count.

SAFETY (box is 12 GiB, had a prior OOM runaway): this test drives a MODERATE
tier -- tens to low hundreds of thousands of rows, high distinct-source
cardinality, well under ~2 GiB -- entirely in-process, and asserts the
bounded-state INVARIANTS themselves (cache byte bound via `PoolCache.stats()`,
collector size via `RouteDiagnostics`). It does NOT measure fresh-process RSS
flatness across large tiers; that is Task 3.6's bench harness job (per
`docs/plans/PHASE3-C1-BASELINE.md`'s frozen tiers and flatness bound), not
this in-process test's. This file also never touches `_pool_quality.py`'s
DuckDB spill-backed aggregation (that path's own boundedness is Task 3.2's
scope, tested there); "each state owner" here means the two Task 3.4 was
asked to build/prove: `PoolCache` and `RouteDiagnostics`.

Faker-only config throughout, so this never needs the compiled companion
(unlike the hash kernel path in `tests/native/test_kernels_keyed.py`).
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution.native._dispatch import (
    NativeRouteEvidence,
    run_native_or_oracle_chunked,
)
from decoy_engine.execution.native._route_diagnostics import PoolOwner, RouteDiagnostics
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.keyprovider import SecretKeyProvider

_ENGINE_VERSION = "phase3-task3.4-adversary"
_DEFAULT_MAX_BYTES = 256 * 1024 * 1024


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=bytes(range(32)), key_version="v1")


def _faker_config(*, pool_size: int, namespace: str = "adv_ns") -> dict:
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
                        "namespace": namespace,
                        "pool_size": pool_size,
                    }
                ],
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _high_cardinality_source(n_rows: int, n_distinct: int) -> pa.Table:
    values = [f"src_{i % n_distinct}" for i in range(n_rows)]
    return pa.table({"FIRST": pa.array(values, type=pa.string())})


def _chunk(table: pa.Table, batch_size: int) -> list[pa.Table]:
    return [table.slice(i, batch_size) for i in range(0, table.num_rows, batch_size)]


def _run(
    config: dict, source: pa.Table, batch_size: int, cache: PoolCache
) -> tuple[NativeRouteEvidence, RouteDiagnostics]:
    # Constructed BEFORE dispatch: the collector's isolation baseline must
    # predate this invocation's own pool build, exactly like a real
    # per-table coordinator would use it.
    diag = RouteDiagnostics(cache)
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
    return sink[0], diag


def test_row_and_chunk_count_do_not_move_cache_or_collector_state() -> None:
    pool_size = 2_000

    def _drive(
        n_rows: int, n_distinct: int, batch_size: int
    ) -> tuple[NativeRouteEvidence, RouteDiagnostics, PoolCache]:
        config = _faker_config(pool_size=pool_size)
        source = _high_cardinality_source(n_rows, n_distinct)
        cache = PoolCache()  # default 256 MB budget; pool_size 2000 sits far below it
        evidence, diag = _run(config, source, batch_size, cache)
        return evidence, diag, cache

    # Fixed rows, chunk count 4x -> 100x (the chunk-count adversary): the
    # pool builds ONCE per invocation, before the chunk loop, so neither the
    # cache's bytes nor the collector's warning/error counts should move.
    ev_few, diag_few, cache_few = _drive(100_000, 25_000, batch_size=25_000)
    ev_many, diag_many, cache_many = _drive(100_000, 25_000, batch_size=1_000)
    assert ev_few.native_admitted is True
    assert ev_many.native_admitted is True
    assert len(diag_few.pool_warnings()) == 0
    assert len(diag_many.pool_warnings()) == 0
    assert len(diag_few.row_errors()) == 0
    assert len(diag_many.row_errors()) == 0
    assert cache_few.stats().bytes_used == cache_many.stats().bytes_used

    # Fixed chunk size, rows/distinct-sources 10x (the row-count adversary).
    # The pool build depends only on (provider, size, job_seed, locale,
    # config, namespace) -- never on the source column -- so a 10x
    # row/distinct-source jump must not move the cache's byte usage at all;
    # this is the concrete proof PoolCache is O(pool_size), not O(rows).
    ev_small, diag_small, cache_small = _drive(10_000, 2_500, batch_size=1_000)
    ev_large, diag_large, cache_large = _drive(100_000, 25_000, batch_size=1_000)
    assert len(diag_small.pool_warnings()) == 0
    assert len(diag_large.pool_warnings()) == 0
    assert len(diag_small.row_errors()) == 0
    assert len(diag_large.row_errors()) == 0
    assert cache_small.stats().bytes_used == cache_large.stats().bytes_used
    assert cache_small.stats().bytes_capacity == _DEFAULT_MAX_BYTES


def test_dominate_warning_count_stays_at_one_across_chunk_count() -> None:
    pool_size = 2_000
    config = _faker_config(pool_size=pool_size)

    # Measure this identity's real pool_bytes under a generous cache (fixed
    # seed 20260830 makes the build fully deterministic; no guessing at real
    # Faker string lengths), then size a small cache so this ONE pool alone
    # clears the 25% dominate threshold without tripping the capacity error
    # (max_bytes = 3x measured: dominate_threshold = 0.75x measured is below
    # the pool's own size, while 3x measured comfortably exceeds it).
    measuring_cache = PoolCache()
    _run(config, _high_cardinality_source(1_000, 200), 500, measuring_cache)
    measured_bytes = measuring_cache.stats().bytes_used
    assert measured_bytes > 0

    def _drive_with_dominate(
        n_rows: int, n_distinct: int, batch_size: int
    ) -> tuple[NativeRouteEvidence, RouteDiagnostics, PoolCache]:
        cache = PoolCache(max_bytes=measured_bytes * 3)
        source = _high_cardinality_source(n_rows, n_distinct)
        evidence, diag = _run(config, source, batch_size, cache)
        return evidence, diag, cache

    ev_few, diag_few, cache_few = _drive_with_dominate(100_000, 25_000, batch_size=25_000)
    ev_many, diag_many, cache_many = _drive_with_dominate(100_000, 25_000, batch_size=1_000)

    assert ev_few.native_admitted is True
    assert ev_many.native_admitted is True
    # The pool builds once, before the chunk loop, so the dominate warning
    # fires once no matter how many chunks follow it -- the collector's size
    # tracks column count, not chunk count.
    assert len(diag_few.pool_warnings()) == 1
    assert len(diag_many.pool_warnings()) == 1
    assert cache_few.stats().bytes_used <= cache_few.stats().bytes_capacity
    assert cache_many.stats().bytes_used <= cache_many.stats().bytes_capacity


def test_pool_cache_evicts_at_byte_bound_under_many_distinct_identities() -> None:
    """Many distinct namespaces (distinct pool identities) sharing ONE small
    cache force real LRU eviction, proving `PoolCache` never grows past its
    byte bound regardless of identity count -- and that each invocation's own
    `RouteDiagnostics` stays isolated from every other invocation's warnings
    even under this eviction churn (Task 3.4's dual contract in one drive)."""
    pool_size = 500
    max_bytes = 40_000  # fits a handful of ~500-value pools, not all 30
    cache = PoolCache(max_bytes=max_bytes)
    source = _high_cardinality_source(2_000, 500)

    per_invocation_warning_counts = []
    for i in range(30):
        config = _faker_config(pool_size=pool_size, namespace=f"adv_ns_{i}")
        evidence, diag = _run(config, source, 500, cache)
        assert evidence.native_admitted is True
        per_invocation_warning_counts.append(
            len(diag.pool_warnings(owners=[PoolOwner("t", "FIRST", "person_first_name")]))
        )

    stats = cache.stats()
    assert stats.bytes_used <= max_bytes
    assert stats.evictions > 0
    assert stats.entries < 30  # not every identity survives; eviction really happened
    # No invocation's own collector ever saw more than its own single build's
    # warning, regardless of how many of the other 29 invocations shared this
    # cache before or after it -- isolation held under real eviction churn.
    assert all(count <= 1 for count in per_invocation_warning_counts)


def _dom_pool(provider: str, *, size: int):
    """A synthetic float64 pool sized to a precise byte count (8 bytes/value),
    to drive PoolCache's dominate-threshold math deterministically."""
    import numpy as np

    from decoy_engine.generation.pool._value_pool import ValuePool

    return ValuePool(
        values=np.arange(size, dtype=np.float64),
        provider=provider,
        locale="default",
        config_hash="cfg0",
        seed=b"seed_" + provider.encode(),
        size=size,
        build_time_ms=0.0,
        backend_type="test",
        backend_version="0",
        distinct_count=size,
    )


def test_collector_view_stays_bounded_under_same_identity_rebuild_churn() -> None:
    # A pool evicted under budget pressure and rebuilt at the SAME identity
    # re-emits pool_dominates_cache every cycle, so the shared cache's monotonic
    # _warnings list grows O(re-puts). That is a KNOWN pre-existing Phase 0 owner
    # (a process-wide dedup CANNOT fix it without breaking RouteDiagnostics'
    # length-prefix isolation -- it would suppress a re-emission this invocation
    # genuinely produced; the real fix is an emission sequence, tracked as a
    # follow-up). What Task 3.4 owns and MUST keep bounded is the COLLECTOR's
    # per-invocation view: one invocation's deduped output is bounded by the
    # distinct dominating pools it saw, never by the re-put count.
    cache = PoolCache(max_bytes=1000)  # dominate threshold = 250 bytes
    pool_a = _dom_pool("A", size=40)  # 320 bytes > 250 -> dominates
    pool_b = _dom_pool("B", size=100)  # 800 bytes -> dominates AND evicts A

    diag = RouteDiagnostics(cache)
    for _ in range(50):
        cache.put(pool_a)
        cache.put(pool_b)  # evicts A
        cache.put(pool_a)  # evicts B, A re-enters -> re-emits

    # The cache's raw list grew with churn (the pre-existing monotonic owner)...
    assert len(cache.warnings()) > 2
    assert cache.stats().evictions > 0
    # ...but THIS invocation's collector view is bounded to the 2 distinct
    # dominating identities, not ~100 re-emissions.
    view = diag.pool_warnings()
    assert len(view) == 2
    assert sorted({w.warning.provider for w in view}) == ["A", "B"]

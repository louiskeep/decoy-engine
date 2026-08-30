"""Task 3.1: chunked deterministic-faker masking on the native route.

Covers the strategy-set seam (`NATIVE_POOL_STRATEGIES`, distinct from
`NATIVE_KERNEL_STRATEGIES`), the JC-5 admission guard (only deterministic
reuse + namespace + pool_size faker admits; every other faker variant stays
on the oracle), pool-identity sharing (built once per unique identity,
`PoolCache.get`/`build`), the DE-02 seam (mask_key re-keys selection;
job_seed re-keys the pool build), and the `pool_select` route-evidence
counters. End-to-end oracle-vs-native logical parity lives in
`tests/parity/native/test_c1_faker_parity.py`.
"""

from __future__ import annotations

import importlib.util

import pyarrow as pa
import pytest

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution.native._dispatch import (
    NativeRouteEvidence,
    plan_native_route,
    run_native_or_oracle_chunked,
)
from decoy_engine.execution.native._requirements import (
    NATIVE_POOL_STRATEGIES,
    faker_pool_precondition_met,
)
from decoy_engine.generation.pool import PoolBuilder, PoolCache
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.profile import ColumnProfile, Profile, TableProfile

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)

_ENGINE_VERSION = "phase3-task3.1"
_SECRET_A = bytes(range(32))
_SECRET_B = bytes(range(1, 33))


def _key_provider(secret: bytes = _SECRET_A) -> SecretKeyProvider:
    return SecretKeyProvider(secret=secret, key_version="v1")


def _faker_column(
    name: str = "FIRST",
    *,
    provider: str = "person_first_name",
    deterministic: bool | None = True,
    namespace: str | None = "ns_first",
    pool_size: int | None = 50,
    cardinality_mode: str | None = None,
) -> dict:
    col: dict = {"name": name, "strategy": "faker", "provider": provider}
    if deterministic is not None:
        col["deterministic"] = deterministic
    if namespace is not None:
        col["namespace"] = namespace
    if pool_size is not None:
        col["pool_size"] = pool_size
    if cardinality_mode is not None:
        col["cardinality_mode"] = cardinality_mode
    return col


def _config(*columns: dict, seed: int = 20260830) -> dict:
    raw = {
        "version": 1,
        "global_settings": {"seed": seed, "post_validation": False},
        "sources": {"t": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "targets": {"t": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "tables": [{"name": "t", "columns": list(columns)}],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _source(n: int = 12, *, n_distinct: int = 5) -> pa.Table:
    values = [f"src_{i % n_distinct}" if i % 7 != 0 else None for i in range(n)]
    return pa.table({"FIRST": pa.array(values, type=pa.string())})


def _profile(name: str, dtype: str = "object") -> Profile:
    from datetime import datetime

    col = ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=3,
        null_count=0,
        distinct_count=3,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=3, columns=(col,)),),
        relationships=(),
        profiled_at=datetime(2026, 8, 30, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _chunk(table: pa.Table, batch_size: int) -> list[pa.Table]:
    return [table.slice(i, batch_size) for i in range(0, table.num_rows, batch_size)]


def _run_native(
    config: dict, source: pa.Table, batch_size: int, *, pool_cache: PoolCache | None = None
) -> tuple[pa.Table, NativeRouteEvidence]:
    sink: list[NativeRouteEvidence] = []
    chunks = list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, batch_size),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(),
            route_evidence_sink=sink,
            pool_cache=pool_cache,
        )
    )
    return pa.concat_tables(chunks).combine_chunks(), sink[0]


# ---------------------------------------------------------------------------
# Strategy-set seam + JC-5 admission guard.
# ---------------------------------------------------------------------------


def test_deterministic_reuse_faker_admits_on_native_pool_route() -> None:
    config = _config(_faker_column())
    source = _source()
    _, evidence = _run_native(config, source, batch_size=4)

    assert evidence.native_admitted is True
    assert evidence.reroute_reason is None
    assert {r.column: r.route for r in evidence.node_routes} == {"FIRST": "native_pool"}


@pytest.mark.parametrize(
    "columns",
    [
        pytest.param([_faker_column(deterministic=False)], id="non_deterministic"),
        pytest.param([_faker_column(deterministic=None)], id="deterministic_omitted"),
        pytest.param(
            [_faker_column(cardinality_mode="unique")], id="unique_cardinality_deterministic"
        ),
        pytest.param(
            [_faker_column(cardinality_mode="match_source_cardinality")],
            id="match_cardinality_deterministic",
        ),
        pytest.param(
            [_faker_column(cardinality_mode="scale_source_cardinality")],
            id="scale_cardinality_deterministic",
        ),
        pytest.param([_faker_column(pool_size=None)], id="missing_pool_size"),
    ],
)
def test_non_c1_faker_variant_stays_on_oracle(columns: list[dict]) -> None:
    # Admission-level check only (`plan_native_route`), not a full chunked
    # run: `run_mask_pipeline_chunked` (the oracle fallback) has its OWN,
    # separate, pre-existing chunk-safety gate that HARD-rejects a
    # non-deterministic/non-C1 faker column before compiling at all (`faker`
    # is not in `CHUNK_SAFE_STRATEGIES`) -- exactly the JC-5 point that the
    # non-deterministic default C1 recipe cannot run chunked at all, on
    # EITHER route. That pre-existing gate is out of Task 3.1's scope; this
    # test only proves the NATIVE admission decision itself stays narrow.
    config = _config(*columns)
    decision = plan_native_route(
        config, _profile("FIRST"), table="t", engine_version=_ENGINE_VERSION
    )

    assert decision.native_admitted is False
    assert decision.reroute_reason is not None
    assert decision.reroute_reason.startswith("fallback_policy_not_native:FIRST:python_only")
    assert all(r.route == "oracle" for r in decision.node_routes)


def test_non_string_faker_source_reroutes_whole_table_to_oracle() -> None:
    # Codex HIGH (Task 3.1): native faker selection converts the source with
    # per-chunk `source.to_pandas()`, which diverges from the oracle's
    # table-level conversion for non-string nullable types. A nullable Int64
    # source can materialize as float64 in a later chunk (3 -> 3.0), so
    # deterministic canonicalization raises `float_canonicalization_unsupported`
    # AFTER earlier chunks already yielded -- partial native output. The whole
    # table must reroute to the oracle (C1's faker columns are string-typed).
    config = _config(_faker_column())  # C1-valid faker config: admits if string
    source = pa.table({"FIRST": pa.array([1, 2, None, 3, 4, 5], type=pa.int64())})
    sink: list[NativeRouteEvidence] = []
    # Eager preflight appends the decision before any chunk is masked; discard
    # the iterator so the oracle fallback never runs (this asserts the route
    # DECISION, not the oracle's own Int64 handling).
    run_native_or_oracle_chunked(
        config,
        _chunk(source, 2),
        table="t",
        engine_version=_ENGINE_VERSION,
        key_provider=_key_provider(),
        route_evidence_sink=sink,
    )
    evidence = sink[0]
    assert evidence.native_admitted is False
    assert evidence.reroute_reason is not None
    assert evidence.reroute_reason.startswith("faker_source_type_not_string:FIRST:")
    assert all(r.route == "oracle" for r in evidence.node_routes)
    assert evidence.compiled_kernel_executed is False


def test_large_string_faker_source_still_admits_native() -> None:
    # The scope-lock admits BOTH string kinds: a large_utf8 source is still a
    # string source, so it must not be rerouted by the non-string guard.
    config = _config(_faker_column())
    values = [f"id-{i % 5}" for i in range(12)]
    source = pa.table({"FIRST": pa.array(values, type=pa.large_string())})
    sink: list[NativeRouteEvidence] = []
    run_native_or_oracle_chunked(
        config,
        _chunk(source, 4),
        table="t",
        engine_version=_ENGINE_VERSION,
        key_provider=_key_provider(),
        route_evidence_sink=sink,
    )
    assert sink[0].native_admitted is True
    assert {r.column: r.route for r in sink[0].node_routes} == {"FIRST": "native_pool"}


def test_missing_namespace_on_deterministic_column_never_reaches_native_admission() -> None:
    # A deterministic column with no namespace never reaches
    # `faker_pool_precondition_met` at all: `compile_plan`'s own namespace
    # binder (relationships/_namespace.py) already hard-rejects it before
    # `compile_native_plan` runs. The precondition's own namespace check
    # (_requirements.py) is defense-in-depth for this same rule, not the
    # reachable gate for this exact combination.
    from decoy_engine.relationships._namespace import NamespaceConfigError

    config = _config(_faker_column(namespace=None))
    with pytest.raises(NamespaceConfigError):
        plan_native_route(config, _profile("FIRST"), table="t", engine_version=_ENGINE_VERSION)


def test_faker_never_folded_into_native_kernel_strategies() -> None:
    # Task 3.1 Step 1: a SEPARATE frozenset, not an overload of the "a
    # compiled scalar kernel exists" constant.
    from decoy_engine.execution.native._requirements import NATIVE_KERNEL_STRATEGIES

    assert "faker" not in NATIVE_KERNEL_STRATEGIES
    assert "faker" in NATIVE_POOL_STRATEGIES


def test_faker_pool_precondition_met_rejects_composite_group_node() -> None:
    # A non-ColumnSeed plan_slice (a composite/FK-group node) never satisfies
    # the precondition, regardless of its attributes -- composites have their
    # own admission path.
    class _FakeGroupNode:
        plan_slice = object()

    assert faker_pool_precondition_met(_FakeGroupNode()) is False


@_NEEDS_COMPANION
def test_mixed_faker_and_hash_all_admit_when_faker_is_c1_variant() -> None:
    config = _config(
        _faker_column(),
        {"name": "SSN", "strategy": "hash", "namespace": "ssn_identity"},
    )
    source = pa.table(
        {
            "FIRST": pa.array(["a", "b", None, "a"], type=pa.string()),
            "SSN": pa.array(["111-22-3333", "222-33-4444", None, "444-55-6666"], type=pa.string()),
        }
    )
    _, evidence = _run_native(config, source, batch_size=2)

    assert evidence.native_admitted is True
    routed = {r.column: r.route for r in evidence.node_routes}
    assert routed == {"FIRST": "native_pool", "SSN": "native_kernel"}


def test_one_non_c1_faker_column_reroutes_whole_table_not_just_that_column() -> None:
    # Admission-level check (see the comment on test_non_c1_faker_variant_stays_
    # on_oracle for why this does not exercise the full chunked oracle fallback).
    config = _config(
        _faker_column("FIRST"),
        _faker_column("LAST", provider="person_last_name", deterministic=False, namespace="ns_l"),
    )
    decision = plan_native_route(
        config, _profile("FIRST"), table="t", engine_version=_ENGINE_VERSION
    )

    assert decision.native_admitted is False
    assert all(r.route == "oracle" for r in decision.node_routes)
    routed_strategies = {r.column: r.strategy for r in decision.node_routes}
    assert routed_strategies == {"FIRST": "faker", "LAST": "faker"}


# ---------------------------------------------------------------------------
# Route evidence: `pool_select` proves a real chunk selection ran.
# ---------------------------------------------------------------------------


def test_pool_select_counters_prove_real_invocation_not_intent() -> None:
    config = _config(_faker_column())
    source = _source(n=12)
    sink: list[NativeRouteEvidence] = []
    it = run_native_or_oracle_chunked(
        config,
        _chunk(source, 4),
        table="t",
        engine_version=_ENGINE_VERSION,
        key_provider=_key_provider(),
        route_evidence_sink=sink,
    )
    evidence = sink[0]
    assert evidence.pool_select_executed is False
    assert evidence.pool_select_calls == 0
    list(it)
    assert evidence.pool_select_executed is True
    # 3 chunks (batch_size=4, n=12) x 1 faker column = 3 selection calls.
    assert evidence.pool_select_calls == 3
    assert evidence.kernel_calls.get("faker") == 3
    # No compiled kernel ran (no hash column in this config).
    assert evidence.compiled_kernel_executed is False


def test_pool_select_counts_per_column_chunk_with_two_faker_columns() -> None:
    config = _config(
        _faker_column("FIRST"),
        _faker_column("LAST", provider="person_last_name", namespace="ns_last"),
    )
    source = pa.table(
        {
            "FIRST": pa.array(["a", "b", "c", "a", "b", "c"], type=pa.string()),
            "LAST": pa.array(["x", "y", "z", "x", "y", "z"], type=pa.string()),
        }
    )
    _, evidence = _run_native(config, source, batch_size=2)
    # 3 chunks x 2 faker columns = 6 pool_select calls.
    assert evidence.pool_select_calls == 6


# ---------------------------------------------------------------------------
# Pool identity + cache correctness (HIGH 1): built once per unique identity,
# shared across chunks and across columns that resolve to the same identity.
# ---------------------------------------------------------------------------


def test_pool_built_once_per_invocation_regardless_of_chunk_count() -> None:
    config = _config(_faker_column())
    source = _source(n=20)
    cache = PoolCache()
    _run_native(config, source, batch_size=3, pool_cache=cache)
    stats = cache.stats()
    assert stats.entries == 1
    # One miss (the first build) and zero further misses; get() is never
    # called again per chunk because the pool is resolved ONCE before the
    # chunk loop (Step 2), not per-chunk.
    assert stats.misses == 1


def test_warm_cache_reused_across_separate_invocations() -> None:
    config = _config(_faker_column())
    cache = PoolCache()
    _run_native(config, _source(n=8), batch_size=3, pool_cache=cache)
    first_stats = cache.stats()
    assert first_stats.misses == 1

    # A second, separate run_native_or_oracle_chunked call sharing the SAME
    # cache and the SAME identity-determining inputs (config, job_seed) must
    # hit the warm cache rather than rebuild.
    _run_native(config, _source(n=8), batch_size=4, pool_cache=cache)
    second_stats = cache.stats()
    assert second_stats.entries == 1
    assert second_stats.hits == first_stats.hits + 1
    assert second_stats.misses == first_stats.misses  # no new build


def test_reversed_table_order_still_hits_warm_cache() -> None:
    config = _config(_faker_column())
    cache = PoolCache()
    source = _source(n=10)
    reversed_source = source.take(pa.array(list(reversed(range(source.num_rows)))))
    _run_native(config, source, batch_size=3, pool_cache=cache)
    _run_native(config, reversed_source, batch_size=3, pool_cache=cache)
    assert cache.stats().entries == 1
    assert cache.stats().misses == 1


def test_distinct_namespaces_build_distinct_pools() -> None:
    config = _config(
        _faker_column("FIRST", namespace="ns_a"),
        _faker_column("LAST", provider="person_first_name", namespace="ns_b"),
    )
    source = pa.table(
        {
            "FIRST": pa.array(["a", "b", "c"], type=pa.string()),
            "LAST": pa.array(["a", "b", "c"], type=pa.string()),
        }
    )
    cache = PoolCache()
    result, _ = _run_native(config, source, batch_size=2, pool_cache=cache)
    assert cache.stats().entries == 2
    # Same provider + source values, different namespace -> different pool
    # -> the two columns need not (and, with a shared provider vocabulary
    # this small, typically do not) sample identically.
    first_vals = result.column("FIRST").to_pylist()
    last_vals = result.column("LAST").to_pylist()
    assert first_vals != last_vals


def test_repeated_provider_across_columns_same_namespace_shares_one_pool() -> None:
    # Same provider, same namespace, same pool_size/config -> ONE identity,
    # even though the columns are named differently.
    config = _config(
        _faker_column("FIRST", namespace="ns_shared"),
        _faker_column("LAST", provider="person_first_name", namespace="ns_shared"),
    )
    source = pa.table(
        {
            "FIRST": pa.array(["a", "b", "c"], type=pa.string()),
            "LAST": pa.array(["a", "b", "c"], type=pa.string()),
        }
    )
    cache = PoolCache()
    result, _ = _run_native(config, source, batch_size=2, pool_cache=cache)
    assert cache.stats().entries == 1
    # Same identity + same DE-02 select_seed (mask_key) + same source values
    # -> byte-identical selections for both columns.
    assert result.column("FIRST").to_pylist() == result.column("LAST").to_pylist()


def test_forced_eviction_still_rebuilds_value_identical_pool() -> None:
    # A pool larger than the tiny budget forces eviction of the FIRST
    # column's pool before the LAST column's identity is resolved; a later
    # re-consult for FIRST's identity must rebuild it, byte-identically
    # (deterministic build), not silently reuse LAST's evicted slot.
    config = _config(
        _faker_column("FIRST", namespace="ns_a", pool_size=300),
        _faker_column("LAST", provider="person_first_name", namespace="ns_b", pool_size=300),
    )
    source = pa.table(
        {
            "FIRST": pa.array(["a", "b", "c"], type=pa.string()),
            "LAST": pa.array(["a", "b", "c"], type=pa.string()),
        }
    )
    # Each pool alone is ~7.3 KB; a 10 KB budget fits one but not both, so
    # inserting LAST's pool evicts FIRST's.
    tiny_cache = PoolCache(max_bytes=10_000)
    result, _ = _run_native(config, source, batch_size=2, pool_cache=tiny_cache)
    assert tiny_cache.stats().evictions >= 1

    # Re-run against a fresh, unbounded cache: FIRST's selections must be
    # byte-identical to the evicting run (rebuild is deterministic).
    fresh_cache = PoolCache()
    result_fresh, _ = _run_native(config, source, batch_size=2, pool_cache=fresh_cache)
    assert result.column("FIRST").to_pylist() == result_fresh.column("FIRST").to_pylist()


def test_build_config_key_ordering_does_not_change_pool_identity() -> None:
    # `resolve_faker_pool_identity` hashes `build_config` via
    # `json.dumps(..., sort_keys=True)` (`_builder.py::_config_hash`), so two
    # `provider_config` dicts with the same keys in different insertion order
    # must resolve to the SAME identity -- one cache entry, one pool. Uses
    # `resolve_faker_pool_identity` directly (identity computation only, no
    # real Faker call) since the actual provider method would reject an
    # arbitrary extra kwarg regardless of ordering.
    from decoy_engine.generation.pool._identity import resolve_faker_pool_identity
    from decoy_engine.providers_v2 import get_default_registry

    builder = PoolBuilder(get_default_registry())
    job_seed = (20260830).to_bytes(8, "big")
    _, _, _, identity_a = resolve_faker_pool_identity(
        builder=builder,
        provider="person_first_name",
        plan_pool_size=40,
        namespace="ns_order",
        job_seed=job_seed,
        cfg={"birthdate": "1990-01-01", "unused_marker": "x"},
    )
    _, _, _, identity_b = resolve_faker_pool_identity(
        builder=builder,
        provider="person_first_name",
        plan_pool_size=40,
        namespace="ns_order",
        job_seed=job_seed,
        cfg={"unused_marker": "x", "birthdate": "1990-01-01"},
    )
    assert identity_a == identity_b


def test_explicit_null_pool_size_falls_back_to_provider_config_or_default() -> None:
    # `resolve_faker_pool_identity`'s raw-config fallback (only reachable for
    # a hand-built ColumnSeed that bypassed `compile_plan`, e.g. a direct
    # caller of `_resolve_faker_pools`; native admission itself always
    # requires a compiled, non-None `pool_size`). An explicit `pool_size:
    # None` in `provider_config` coalesces to the shared default, exactly
    # like an absent key (`resolve_runtime_pool_size`).
    from decoy_engine.execution.native._dispatch import _resolve_faker_pools
    from decoy_engine.generation.pool._runtime_pool_size import DEFAULT_POOL_SIZE
    from decoy_engine.plan._types import ColumnSeed

    seed = ColumnSeed(
        namespace="ns_null_pool_size",
        strategy="faker",
        provider="person_first_name",
        backend_type="faker",
        backend_version="",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("pool_size", None),),
        pool_size=None,  # ColumnSeed.pool_size itself unset too
    )
    pools = _resolve_faker_pools(
        {"c1": seed}, job_seed=(1).to_bytes(8, "big"), pool_cache=PoolCache()
    )
    assert len(pools["c1"].values) == DEFAULT_POOL_SIZE


# ---------------------------------------------------------------------------
# DE-02 seam: mask_key re-keys SELECTION only; job_seed re-keys the BUILD.
# ---------------------------------------------------------------------------


def _run_with_key(config: dict, source: pa.Table, key_provider: SecretKeyProvider) -> pa.Table:
    sink: list[NativeRouteEvidence] = []
    chunks = list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, 3),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=key_provider,
            route_evidence_sink=sink,
        )
    )
    return pa.concat_tables(chunks).combine_chunks()


@pytest.mark.parametrize("batch_size", [1, 3, 7])
def test_mask_key_only_changes_selection_not_pool_identity(batch_size: int) -> None:
    config = _config(_faker_column())
    source = _source(n=9)

    cache = PoolCache()
    result_a = _run_native(config, source, batch_size, pool_cache=cache)[0]
    identity_a = set(cache._entries)  # test-only identity introspection

    cache2 = PoolCache()
    sink: list[NativeRouteEvidence] = []
    chunks = list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, batch_size),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(_SECRET_B),
            route_evidence_sink=sink,
            pool_cache=cache2,
        )
    )
    result_b = pa.concat_tables(chunks).combine_chunks()
    identity_b = set(cache2._entries)

    # Same pool identity (build stays on job_seed, unaffected by mask_key)...
    assert identity_a == identity_b
    # ...but different deterministic selections (select_seed = mask_key).
    assert result_a.column("FIRST").to_pylist() != result_b.column("FIRST").to_pylist()


@pytest.mark.parametrize("cache_state", ["cold", "warm"])
def test_job_seed_only_changes_pool_identity_and_build(cache_state: str) -> None:
    config_a = _config(_faker_column(), seed=111)
    config_b = _config(_faker_column(), seed=222)
    source = _source(n=9)

    shared_cache = PoolCache() if cache_state == "warm" else None
    cache_a = shared_cache if shared_cache is not None else PoolCache()
    cache_b = shared_cache if shared_cache is not None else PoolCache()

    _run_native(config_a, source, batch_size=3, pool_cache=cache_a)
    before = dict((cache_b if cache_state == "warm" else cache_a)._entries)
    _run_native(config_b, source, batch_size=3, pool_cache=cache_b)
    after = dict(cache_b._entries)

    # A different job_seed resolves a different pool_seed inside identity_for
    # (S5 spec Sec3.1), so the two configs' pools land on DIFFERENT identity
    # keys -- the cache grows rather than reusing an entry.
    assert set(after) - set(before) or len(after) > len(before)


# ---------------------------------------------------------------------------
# Route decision agrees with the config-only helper (`plan_native_route`).
# ---------------------------------------------------------------------------


def test_plan_native_route_agrees_with_run_native_or_oracle_chunked() -> None:
    config = _config(_faker_column())
    profile = _profile("FIRST")
    decision = plan_native_route(config, profile, table="t", engine_version=_ENGINE_VERSION)
    assert decision.native_admitted is True

    _, evidence = _run_native(config, _source(), batch_size=4)
    assert evidence.native_admitted is decision.native_admitted

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
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution import run_pipeline
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


# ---------------------------------------------------------------------------
# Masking output: the faker branch SELECTS values, so grade the selection
# against the pandas oracle -- never a native-produced golden. The oracle
# comparison needs a real on-disk source (`profile_source` reads the
# CONFIGURED path), unlike `run_native_or_oracle_chunked`, which never reads
# the path (`_ENGINE_VERSION`'s in-memory chunks bypass it entirely).
# ---------------------------------------------------------------------------

_ON_DISK_SOURCE_DIR = tempfile.mkdtemp(prefix="p3t3-dispatch-oracle-")
_on_disk_source_counter = 0


def _config_on_disk(source: pa.Table, *columns: dict, seed: int = 20260830) -> dict:
    global _on_disk_source_counter
    _on_disk_source_counter += 1
    path = f"{_ON_DISK_SOURCE_DIR}/src_{_on_disk_source_counter}.parquet"
    pq.write_table(source, path)
    raw = {
        "version": 1,
        "global_settings": {"seed": seed, "post_validation": False},
        "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": path + ".out"}},
        "tables": [{"name": "t", "columns": list(columns)}],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _run_oracle(config: dict, source: pa.Table, key_provider: SecretKeyProvider) -> pa.Table:
    result = run_pipeline(
        config,
        {"t": source},
        engine_version=_ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        key_provider=key_provider,
        use_byte_estimate_routing=False,
        use_probe_routing=False,
    )
    return result.outputs["t"]


def _null_dense_source(n: int = 24, *, n_distinct: int = 4) -> pa.Table:
    # Repeated keys (n_distinct << n, so every value collides many times) AND
    # a dense, patterned null shape: every 3rd row null, plus the very first
    # and last rows, so null-restore is exercised at both boundaries and
    # mid-stream.
    values = []
    for i in range(n):
        if i % 3 == 0 or i in (0, n - 1):
            values.append(None)
        else:
            values.append(f"src_{i % n_distinct}")
    return pa.table({"FIRST": pa.array(values, type=pa.string())})


@pytest.mark.parametrize("batch_size", [1, 5, 24])
def test_selected_values_match_pandas_oracle_over_repeated_and_null_source(
    batch_size: int,
) -> None:
    # Kills mutants that change a selected value, the selection seed (or its
    # separation from the pool-build seed), the deterministic mode/namespace/
    # scale fed to PoolSampler, or the resolved pool identity: any of those
    # would diverge the native selection from the oracle's, which shares
    # none of `_dispatch.py`'s code (FakerStrategyHandler.run is a completely
    # separate implementation converged only via resolve_faker_pool_identity).
    source = _null_dense_source()
    config = _config_on_disk(source, _faker_column())
    key_provider = _key_provider()

    oracle_out = _run_oracle(config, source, key_provider)
    sink: list[NativeRouteEvidence] = []
    native_chunks = list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, batch_size),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=key_provider,
            route_evidence_sink=sink,
        )
    )
    assert sink[0].native_admitted is True
    native_out = pa.concat_tables(native_chunks).combine_chunks()

    assert native_out.column("FIRST").to_pylist() == oracle_out.column("FIRST").to_pylist()


def test_null_positions_preserved_byte_for_byte_vs_oracle_null_dense_source() -> None:
    # The positional null-restore, isolated from the value-selection check
    # above: exact True/False null-mask agreement at every row, over a chunk
    # boundary that splits a run of nulls (batch_size=3 vs. the null-every-3rd
    # pattern in `_null_dense_source`).
    source = _null_dense_source(n=30, n_distinct=6)
    config = _config_on_disk(source, _faker_column())
    key_provider = _key_provider()

    oracle_out = _run_oracle(config, source, key_provider)
    sink: list[NativeRouteEvidence] = []
    native_chunks = list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, 7),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=key_provider,
            route_evidence_sink=sink,
        )
    )
    native_out = pa.concat_tables(native_chunks).combine_chunks()

    oracle_nulls = [v is None for v in oracle_out.column("FIRST").to_pylist()]
    native_nulls = [v is None for v in native_out.column("FIRST").to_pylist()]
    assert native_nulls == oracle_nulls
    # The source's own null positions are the ground truth both routes must
    # restore: a mask that drifted from the SOURCE (not just from the oracle)
    # would be a coincidental agreement, not a proven positional restore.
    source_nulls = [v is None for v in source.column("FIRST").to_pylist()]
    assert native_nulls == source_nulls


def test_faker_output_column_is_arrow_string_type() -> None:
    # `_sample_faker_chunk` builds `pa.array(..., type=pa.string())`
    # explicitly; a mutant dropping or changing that type argument would let
    # pyarrow infer a different (or null) type instead.
    config = _config(_faker_column())
    result, evidence = _run_native(config, _source(n=6), batch_size=3)
    assert evidence.native_admitted is True
    assert result.column("FIRST").type == pa.string()


def test_pool_cache_hit_selection_is_byte_identical_to_the_cold_build() -> None:
    # A cache HIT must select from the exact same pool object a cache MISS
    # would have built -- not a fresh, possibly-different rebuild, and not a
    # stale/wrong entry from a prior identity. Same config/source/key on a
    # cold cache vs. a pre-warmed one (warmed by an earlier, distinct source
    # so this run is provably a hit, not an accidental first build).
    config = _config(_faker_column())
    source = _source(n=9)
    key_provider = _key_provider()

    cold_cache = PoolCache()
    cold_sink: list[NativeRouteEvidence] = []
    cold_chunks = list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, 4),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=key_provider,
            route_evidence_sink=cold_sink,
            pool_cache=cold_cache,
        )
    )
    cold_out = pa.concat_tables(cold_chunks).combine_chunks()
    assert cold_cache.stats().misses == 1

    warm_cache = PoolCache()
    # Warm the SAME identity via a throwaway run over a DIFFERENT source, then
    # rerun over the real source: the second run must be a cache hit.
    list(
        run_native_or_oracle_chunked(
            config,
            _chunk(_source(n=9, n_distinct=3), 4),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=key_provider,
            route_evidence_sink=[],
            pool_cache=warm_cache,
        )
    )
    assert warm_cache.stats().misses == 1
    warm_sink: list[NativeRouteEvidence] = []
    warm_chunks = list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, 4),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=key_provider,
            route_evidence_sink=warm_sink,
            pool_cache=warm_cache,
        )
    )
    warm_out = pa.concat_tables(warm_chunks).combine_chunks()
    assert warm_cache.stats().misses == 1  # no second build; the hit reused the pool
    assert warm_cache.stats().hits >= 1

    assert warm_out.column("FIRST").to_pylist() == cold_out.column("FIRST").to_pylist()


@_NEEDS_COMPANION
def test_faker_source_type_reject_reroutes_whole_table_including_hash_column() -> None:
    # The preflight string-type guard's "whole table" claim needs a
    # MULTI-STRATEGY case: a single-column config can't distinguish "the bad
    # faker column rerouted" from "the whole table rerouted," since there is
    # nothing else to check. Mixing in an admitted hash column proves the
    # native_kernel route is ALSO downgraded, not just the offending faker
    # column.
    config = _config(
        _faker_column(),
        {"name": "SSN", "strategy": "hash", "namespace": "ssn_identity"},
    )
    source = pa.table(
        {
            # No nulls here on purpose: the oracle fallback this reroute lands
            # on ALSO cannot canonicalize a float-upcast (nullable Int64 with
            # nulls converts to float64 under `Table.to_pandas()`), so an
            # all-present int64 column isolates the ROUTE-level assertion
            # (whole-table reroute) from that separate, non-native-specific
            # deterministic-faker limitation.
            "FIRST": pa.array([1, 2, 3, 1], type=pa.int64()),  # not string -> reroute
            "SSN": pa.array(
                ["111-22-3333", "222-33-4444", "333-44-5555", "444-55-6666"], type=pa.string()
            ),
        }
    )
    sink: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, 2),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is False
    assert evidence.reroute_reason is not None
    assert evidence.reroute_reason.startswith("faker_source_type_not_string:FIRST:")
    routed = {r.column: r.route for r in evidence.node_routes}
    assert routed == {"FIRST": "oracle", "SSN": "oracle"}
    assert evidence.compiled_kernel_executed is False


def test_faker_source_type_reject_still_reroutes_when_a_kernel_column_precedes_it() -> None:
    # The scope-lock loop is `for node in decision.node_routes: if node.
    # strategy != "faker": continue`. A non-faker column appearing BEFORE the
    # bad-typed faker column in `node_routes` order must be skipped over, not
    # treated as a reason to stop looking -- the previous test put the faker
    # column first, which cannot tell "skip past" apart from "stop at."
    # passthrough (not hash) keeps this companion-free.
    config = _config(
        {"name": "A", "strategy": "passthrough"},
        _faker_column("FIRST"),
    )
    source = pa.table(
        {
            "A": pa.array(["x", "y", "z", "w"], type=pa.string()),
            "FIRST": pa.array([1, 2, 3, 1], type=pa.int64()),  # not string -> reroute
        }
    )
    sink: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            config,
            _chunk(source, 2),
            table="t",
            engine_version=_ENGINE_VERSION,
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is False
    assert evidence.reroute_reason is not None
    assert evidence.reroute_reason.startswith("faker_source_type_not_string:FIRST:")
    assert {r.column: r.route for r in evidence.node_routes} == {"A": "oracle", "FIRST": "oracle"}


# ---------------------------------------------------------------------------
# `_resolve_faker_pools`: the pool-build loop and the cache-hit/rebuild
# decision, isolated from the full dispatch path.
# ---------------------------------------------------------------------------


def test_resolve_faker_pools_does_not_abort_on_a_non_faker_column_before_it() -> None:
    # `for name, col_seed in col_seed_by_name.items(): if col_seed.strategy
    # != "faker": continue`. A non-faker column ordered BEFORE a faker column
    # in the dict must be skipped over, not treated as a reason to stop --
    # every existing config in this file puts the faker column first, which
    # cannot distinguish `continue` from `break` (nothing follows it either
    # way). `_resolve_faker_pools` is exercised directly (not through the
    # full route) to isolate the pool-loop's own control flow.
    from decoy_engine.execution.native._dispatch import _resolve_faker_pools
    from decoy_engine.plan._types import ColumnSeed

    hash_seed = ColumnSeed(
        namespace="ns_hash",
        strategy="hash",
        provider=None,
        backend_type=None,
        backend_version="",
        cardinality_mode=None,
        deterministic=False,
        provider_config=(),
        pool_size=None,
    )
    faker_seed = ColumnSeed(
        namespace="ns_faker_after_hash",
        strategy="faker",
        provider="person_first_name",
        backend_type="faker",
        backend_version="",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        pool_size=20,
    )
    # dict preserves insertion order: "SSN" (non-faker) is visited BEFORE
    # "FIRST" (faker).
    col_seed_by_name = {"SSN": hash_seed, "FIRST": faker_seed}
    pools = _resolve_faker_pools(
        col_seed_by_name, job_seed=(11).to_bytes(8, "big"), pool_cache=PoolCache()
    )
    assert set(pools) == {"FIRST"}
    assert len(pools["FIRST"].values) == 20


def test_resolve_faker_pools_reuses_the_exact_cached_object_on_a_hit() -> None:
    # A cache HIT must return the SAME pool object `PoolCache.get()` found,
    # not discard it and rebuild -- a rebuild is value-identical (the build is
    # deterministic) but defeats the cache's whole purpose, and no VALUE-based
    # assertion can tell "reused" apart from "value-identically rebuilt."
    # Object identity is the only thing that can.
    from decoy_engine.execution.native._dispatch import _resolve_faker_pools
    from decoy_engine.plan._types import ColumnSeed

    seed = ColumnSeed(
        namespace="ns_hit_identity",
        strategy="faker",
        provider="person_first_name",
        backend_type="faker",
        backend_version="",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        pool_size=15,
    )
    cache = PoolCache()
    job_seed = (7).to_bytes(8, "big")
    first_pools = _resolve_faker_pools({"c1": seed}, job_seed=job_seed, pool_cache=cache)
    built_pool = first_pools["c1"]
    assert cache.stats().misses == 1

    second_pools = _resolve_faker_pools({"c1": seed}, job_seed=job_seed, pool_cache=cache)
    assert cache.stats().hits == 1
    assert cache.stats().misses == 1  # no second build
    assert second_pools["c1"] is built_pool


def test_resolve_faker_pools_passes_locale_and_config_through_to_the_builder(monkeypatch) -> None:
    # `builder.build(..., locale=locale, config=build_config, ...)`: both are
    # real determinants (they feed `cfg_hash`/`pool_seed`, i.e. pool identity
    # and content), but every OTHER test in this file uses the default
    # locale and an empty provider_config, so a mutant dropping either kwarg
    # (or forcing it to None) is indistinguishable from correct code under
    # those tests alone. Spy on `PoolBuilder.build` to capture what actually
    # gets passed -- this sidesteps needing a real Faker provider method that
    # happens to accept an arbitrary extra config kwarg (most reject one).
    import numpy as np

    from decoy_engine.execution.native._dispatch import _resolve_faker_pools
    from decoy_engine.generation.pool._builder import PoolBuilder
    from decoy_engine.generation.pool._value_pool import ValuePool
    from decoy_engine.plan._types import ColumnSeed

    captured: dict = {}

    def _spy_build(self, provider, **kwargs):
        captured.update(kwargs)
        return ValuePool(
            values=np.array(["stub_a", "stub_b"], dtype=object),
            provider=provider,
            locale=kwargs.get("locale") or "default",
            config_hash="test-hash",
            seed=b"test-seed",
            size=2,
            build_time_ms=0.0,
            backend_type="test",
            backend_version="0",
            distinct_count=2,
        )

    monkeypatch.setattr(PoolBuilder, "build", _spy_build)

    seed = ColumnSeed(
        namespace="ns_spy",
        strategy="faker",
        provider="person_first_name",
        backend_type="faker",
        backend_version="",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("locale", "en_US"), ("unused_marker", "x")),
        pool_size=20,
    )
    _resolve_faker_pools({"c1": seed}, job_seed=(3).to_bytes(8, "big"), pool_cache=PoolCache())

    assert captured["locale"] == "en_US"
    assert captured["config"] == {"unused_marker": "x"}

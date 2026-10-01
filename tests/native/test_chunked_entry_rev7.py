"""B1 rev7: no guard suppression (a delegating adapter masks what the oracle masks)
and a pool cache whose identity includes the provider binding."""

from __future__ import annotations

import inspect
from typing import Any

import pyarrow as pa

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution import _chunked_oracle
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.native._dispatch import NativeRouteEvidence
from decoy_engine.generation.pool import PoolCache
from decoy_engine.providers_v2 import get_default_registry
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    faker_col,
    key_provider,
    make_config,
    split,
    truncate,
)
from tests.native.test_chunked_entry_gate_findings import _DateAdapter


class _DelegatingAdapter:
    """Opaque custom adapter: forwards everything to the stock pandas adapter."""

    def __init__(self) -> None:
        self.kwargs_seen: list[set[str]] = []

    def run(self, *args: Any, **kwargs: Any) -> Any:
        self.kwargs_seen.append(set(kwargs))
        return PandasExecutionAdapter().run(*args, **kwargs)


class _NoKwargsAdapter:
    """A custom adapter whose `run` takes no `**kwargs`: the loop must not need one."""

    def run(
        self,
        plan: Any,
        sources: Any,
        *,
        registry: Any,
        pool_cache: Any,
        relationship_graph: Any,
        namespace_registry: Any,
        unconfigured_column_policy: Any,
        generate_output_tables: Any = frozenset(),
        key_provider: Any,
        row_offset: int,
        code_set_records: Any,
    ) -> Any:
        return PandasExecutionAdapter().run(
            plan,
            sources,
            registry=registry,
            pool_cache=pool_cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
            unconfigured_column_policy=unconfigured_column_policy,
            generate_output_tables=generate_output_tables,
            key_provider=key_provider,
            row_offset=row_offset,
            code_set_records=code_set_records,
        )


def _run(fn: Any, config: dict[str, Any], chunks: list[pa.Table], **kw: Any) -> list[pa.Table]:
    return list(
        fn(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            **kw,
        )
    )


def _int_then_null_chunks() -> list[pa.Table]:
    return [
        pa.table({"n": pa.array([100, 200, 300], pa.int64())}),
        pa.table({"n": pa.nulls(2)}),
    ]


def test_delegating_adapter_masks_int_then_null_chunk_like_the_oracle() -> None:
    config = make_config([truncate("n", 2)])
    oracle = _run(run_mask_pipeline_chunked, config, _int_then_null_chunks())
    adapter = _DelegatingAdapter()
    actual = _run(run_mask_chunked, config, _int_then_null_chunks(), adapter=adapter)
    assert [t.column("n").to_pylist() for t in actual] == [
        t.column("n").to_pylist() for t in oracle
    ]
    # (c) the loop passes the adapter nothing beyond the public oracle's own call.
    assert all("ingest_guards_run" not in seen for seen in adapter.kwargs_seen)


def test_default_run_mask_chunked_masks_int_then_null_chunk_like_the_oracle() -> None:
    config = make_config([truncate("n", 2)])
    oracle = _run(run_mask_pipeline_chunked, config, _int_then_null_chunks())
    actual = _run(run_mask_chunked, config, _int_then_null_chunks())
    assert [t.column("n").to_pylist() for t in actual] == [
        t.column("n").to_pylist() for t in oracle
    ]


def test_adapter_without_kwargs_works_on_the_oracle_route() -> None:
    config = make_config([truncate("n", 2)])
    source = pa.table({"n": pa.array([100, 200, 300, 400], pa.int64())})
    expected = _run(run_mask_pipeline_chunked, config, split(source, 3))
    actual = _run(run_mask_chunked, config, split(source, 3), adapter=_NoKwargsAdapter())
    assert [t.to_pydict() for t in actual] == [t.to_pydict() for t in expected]


def test_no_ingest_guards_run_keyword_exists_anywhere() -> None:
    assert "ingest_guards_run" not in inspect.signature(PandasExecutionAdapter.run).parameters
    assert not hasattr(_chunked_oracle, "_accepts_ingest_guards_run")
    assert "ingest_guarded" not in inspect.signature(_chunked_oracle._oracle_masked).parameters


def _rebound_registry() -> Any:
    default = get_default_registry()
    return default.override(
        "person_first_name", _DateAdapter(), default.get_capabilities("person_first_name")
    )


def _source() -> pa.Table:
    return pa.table({"f": pa.array(["a", "b", None, "c", "d"], pa.string())})


def _warm_default(cache: PoolCache, config: dict[str, Any]) -> None:
    """Populate `cache` with the default-binding pool through the public entry."""
    _run(run_mask_chunked, config, split(_source(), 2), pool_cache=cache)
    assert cache.stats().entries >= 1


def _oracle_with_cache(config: dict[str, Any], cache: PoolCache, registry: Any) -> list[pa.Table]:
    """The public oracle's own preflight and loop, with a caller-supplied cache.

    `run_mask_pipeline_chunked` takes no cache, so the stale-cache check drives
    the shared helpers it is built from."""
    state = _chunked_oracle._oracle_preflight(
        config,
        split(_source(), 2),
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=registry,
        key_provider=key_provider(),
        pool_cache=cache,
    )
    return list(_chunked_oracle._oracle_masked(state, config=config, table=TABLE))


def test_rebound_provider_does_not_reuse_a_stale_pool_on_the_oracle() -> None:
    config = make_config([faker_col("f")])
    registry = _rebound_registry()
    fresh = _oracle_with_cache(config, PoolCache(), registry)
    cache = PoolCache()
    _warm_default(cache, config)
    reused = _oracle_with_cache(config, cache, registry)
    assert [t.to_pydict() for t in reused] == [t.to_pydict() for t in fresh]


@NEEDS_COMPANION
def test_rebound_provider_does_not_reuse_a_stale_pool_on_run_mask_chunked() -> None:
    config = make_config([faker_col("f")])
    registry = _rebound_registry()
    fresh_evidence: list[NativeRouteEvidence] = []
    fresh = _run(
        run_mask_chunked,
        config,
        split(_source(), 2),
        registry=registry,
        pool_cache=PoolCache(),
        route_evidence_sink=fresh_evidence,
    )
    cache = PoolCache()
    _warm_default(cache, config)
    evidence: list[NativeRouteEvidence] = []
    reused = _run(
        run_mask_chunked,
        config,
        split(_source(), 2),
        registry=registry,
        pool_cache=cache,
        route_evidence_sink=evidence,
    )
    assert [t.to_pydict() for t in reused] == [t.to_pydict() for t in fresh]
    assert evidence[0].native_admitted is fresh_evidence[0].native_admitted is False
    assert evidence[0].reroute_reason == fresh_evidence[0].reroute_reason


def test_pool_identity_names_the_provider_binding() -> None:
    from decoy_engine.generation.pool import PoolBuilder

    default = get_default_registry()
    kw: dict[str, Any] = {"size": 8, "job_seed": b"\x01" * 8, "namespace": "n"}
    a = PoolBuilder(default).identity_for("person_first_name", **kw)
    b = PoolBuilder(_rebound_registry()).identity_for("person_first_name", **kw)
    assert a != b
    assert a == PoolBuilder(default).identity_for("person_first_name", **kw)
    # The sampled values do not depend on the binding token: same seed field.
    assert a[3] == b[3]

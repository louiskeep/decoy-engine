"""engine-v2 S9 slice 2b: faker strategy (pool-backed) end-to-end.

Faker routes through PoolBuilder + the vectorized PoolSampler.sample (S9 path
#2). Tests the determinism contract (same job seed -> byte-identical; same
source -> same masked value), null preservation, and that output comes from the
provider's pool.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import ExecutionResult, PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x0123456789).to_bytes(8, "big")


def _faker_plan(*, deterministic: bool, pool_size: int = 256) -> Any:
    cs = ColumnSeed(
        namespace="people_ns" if deterministic else None,
        strategy="faker",
        provider="person_email",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=(("pool_size", pool_size),),
        coherent_with=(),
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("people", TableSeed(per_column=(("email", cs),), per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


class TestFakerStrategy:
    def test_deterministic_reproducible_across_runs(self) -> None:
        src = pa.table({"email": ["a", "b", "a", None]})
        out1 = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        out2 = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        assert out1 == out2

    def test_deterministic_same_source_same_masked_value(self) -> None:
        src = pa.table({"email": ["a", "b", "a"]})
        out = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        assert out[0] == out[2]  # repeated source value -> same masked value

    def test_null_preserved_deterministic(self) -> None:
        src = pa.table({"email": ["a", None, "c"]})
        out = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        assert out[1] is None
        assert out[0] is not None and out[2] is not None

    def test_masked_values_come_from_email_pool(self) -> None:
        src = pa.table({"email": ["a", "b", "c"]})
        out = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        assert all(isinstance(v, str) and "@" in v for v in out)

    def test_non_deterministic_masks_nonnull_preserves_null(self) -> None:
        src = pa.table({"email": ["a", None, "c"]})
        out = _run(_faker_plan(deterministic=False), src).output.column("email").to_pylist()
        assert out[1] is None
        assert isinstance(out[0], str) and "@" in out[0]
        assert isinstance(out[2], str) and "@" in out[2]

"""engine-v2 S9 slice 2d: hash (derive-keyed) + bucketize (no-backend) strategies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, ExecutionResult, PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x55).to_bytes(8, "big")


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(col_name: str, seed: ColumnSeed) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=((col_name, seed),), per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


class TestHash:
    def test_same_source_same_token_and_reproducible(self) -> None:
        src = pa.table({"id": ["alice", "bob", "alice"]})
        out = _run(_plan("id", _col("hash", namespace="ids")), src).output.column("id").to_pylist()
        assert out[0] == out[2]  # joinability: same source -> same token
        assert out[0] != out[1]
        out2 = _run(_plan("id", _col("hash", namespace="ids")), src).output.column("id").to_pylist()
        assert out == out2  # reproducible across runs

    def test_truncate(self) -> None:
        src = pa.table({"id": ["alice"]})
        seed = _col("hash", namespace="ids", provider_config=(("truncate", 12),))
        out = _run(_plan("id", seed), src).output.column("id").to_pylist()
        assert len(out[0]) == 12

    def test_null_preserved(self) -> None:
        src = pa.table({"id": ["alice", None]})
        out = _run(_plan("id", _col("hash", namespace="ids")), src).output.column("id").to_pylist()
        assert out[1] is None

    def test_missing_namespace_raises(self) -> None:
        src = pa.table({"id": ["alice"]})
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("id", _col("hash", namespace=None)), src)
        assert exc.value.code == "hash_requires_namespace"


class TestBucketize:
    def test_lower_format_integer_width(self) -> None:
        src = pa.table({"age": [23, 47, 8, None]})
        seed = _col("bucketize", provider_config=(("width", 10),))
        out = _run(_plan("age", seed), src).output.column("age").to_pylist()
        assert out == ["20", "40", "0", None]

    def test_range_format(self) -> None:
        src = pa.table({"age": [23]})
        seed = _col("bucketize", provider_config=(("width", 10), ("format", "range")))
        out = _run(_plan("age", seed), src).output.column("age").to_pylist()
        assert out == ["20-29"]

    def test_preset_decade(self) -> None:
        src = pa.table({"age": [37]})
        seed = _col("bucketize", provider_config=(("preset", "by_decade"),))
        out = _run(_plan("age", seed), src).output.column("age").to_pylist()
        assert out == ["30"]

    def test_invalid_width_passthrough(self) -> None:
        src = pa.table({"age": [23]})
        seed = _col("bucketize", provider_config=(("width", 0),))
        out = _run(_plan("age", seed), src).output.column("age").to_pylist()
        assert out == [23]

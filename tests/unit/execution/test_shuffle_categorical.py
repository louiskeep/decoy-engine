"""engine-v2 S9 slice 2e: shuffle (derive-seeded rng) + categorical (pool remap)."""

from __future__ import annotations

from collections import Counter
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
_SEED = (0x77).to_bytes(8, "big")


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    deterministic: bool = False,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
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


class TestShuffle:
    def test_preserves_multiset_and_nulls_deterministic(self) -> None:
        src = pa.table({"c": ["a", "b", "c", None, "a"]})
        seed = _col("shuffle", namespace="sh", deterministic=True)
        out = _run(_plan("c", seed), src).output.column("c").to_pylist()
        assert out[3] is None  # null preserved in place
        # multiset of non-null values is preserved (it's a permutation)
        assert Counter(v for v in out if v is not None) == Counter(["a", "b", "c", "a"])

    def test_deterministic_reproducible(self) -> None:
        src = pa.table({"c": ["a", "b", "c", "d", "e"]})
        seed = _col("shuffle", namespace="sh", deterministic=True)
        out1 = _run(_plan("c", seed), src).output.column("c").to_pylist()
        out2 = _run(_plan("c", seed), src).output.column("c").to_pylist()
        assert out1 == out2

    def test_deterministic_requires_namespace(self) -> None:
        src = pa.table({"c": ["a", "b"]})
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("c", _col("shuffle", namespace=None, deterministic=True)), src)
        assert exc.value.code == "shuffle_requires_namespace"


class TestCategorical:
    def test_remaps_into_categories_deterministic(self) -> None:
        src = pa.table({"grade": ["x", "y", "x", None]})
        seed = _col(
            "categorical",
            namespace="g",
            deterministic=True,
            provider_config=(("categories", ["A", "B", "C"]),),
        )
        out = _run(_plan("grade", seed), src).output.column("grade").to_pylist()
        assert out[3] is None
        assert all(v in {"A", "B", "C"} for v in out if v is not None)
        assert out[0] == out[2]  # same source -> same category

    def test_reproducible(self) -> None:
        src = pa.table({"grade": ["x", "y", "z"]})
        seed = _col(
            "categorical",
            namespace="g",
            deterministic=True,
            provider_config=(("categories", ["A", "B", "C", "D"]),),
        )
        out1 = _run(_plan("grade", seed), src).output.column("grade").to_pylist()
        out2 = _run(_plan("grade", seed), src).output.column("grade").to_pylist()
        assert out1 == out2

    def test_requires_categories(self) -> None:
        src = pa.table({"grade": ["x"]})
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("grade", _col("categorical", namespace="g", deterministic=True)), src)
        assert exc.value.code == "categorical_requires_categories"

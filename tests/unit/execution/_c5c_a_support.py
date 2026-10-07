"""Shared builders for the nullable-int deterministic Faker tests.

The plan is a `SimpleNamespace` over a real `SeedEnvelope`, as `test_faker_strategy.py`
does, so a test controls every `ColumnSeed` field (including `when`) without going
through config compilation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.execution import ExecutionResult, PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

REG = get_default_registry()
GRAPH = RelationshipGraph(edges=(), ordering=())
NS = NamespaceRegistry(bindings=())
SEED = (0x0123456789).to_bytes(8, "big")


def faker_seed(
    *,
    provider: str = "person_email",
    deterministic: bool = True,
    when: str | None = None,
    namespace: str | None = "n_ns",
    pool_size: int = 64,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="faker",
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=(("pool_size", pool_size),),
        coherent_with=(),
        when=when,
    )


def seed_of(strategy: str, *, when: str | None = None, **cfg: Any) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=tuple(cfg.items()),
        coherent_with=(),
        when=when,
    )


def plan_of(per_column: dict[str, ColumnSeed], table: str = "t") -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=SEED,
            per_table=((table, TableSeed(per_column=tuple(per_column.items()), per_group=())),),
        )
    )


def run(
    plan: Any,
    table: pa.Table,
    *,
    registry: Any = REG,
    row_offset: int = 0,
    table_name: str = "t",
    namespaces: NamespaceRegistry = NS,
) -> ExecutionResult:
    return PandasExecutionAdapter().run(
        plan,
        {table_name: table},
        registry=registry,
        relationship_graph=GRAPH,
        namespace_registry=namespaces,
        row_offset=row_offset,
    )


def column(result: ExecutionResult, name: str = "n", table: str = "t") -> list[Any]:
    return result.outputs[table].column(name).to_pylist()


def value_map(values: list[Any], masked: list[Any]) -> dict[Any, Any]:
    """Source value to masked value, for the rows whose source value is not null."""
    out: dict[Any, Any] = {}
    for src, got in zip(values, masked, strict=True):
        if src is not None:
            assert out.setdefault(src, got) == got
    return out


class IntAdapter:
    """A poolable adapter whose pool values are integers (a numeric-output provider)."""

    backend_type = "test_int"
    backend_version = "1"

    def __init__(self, provider: str) -> None:
        self._provider = provider

    def generate(self, provider: str, *, spec: Any, source_value: bytes | None = None) -> Any:
        return 7

    def generate_batch(self, provider: str, *, spec: Any, count: int) -> list[int]:
        return list(range(100, 100 + count))

    def capability_matrix(self, provider: str) -> Any:
        return REG.get_capabilities(self._provider)


def int_registry(provider: str = "address_zip") -> Any:
    return REG.override(provider, IntAdapter(provider), REG.get_capabilities(provider))


def arrow_with_index(df: pd.DataFrame) -> pa.Table:
    """An Arrow table whose pandas metadata carries `df`'s (non-default) index."""
    return pa.Table.from_pandas(df, preserve_index=True)

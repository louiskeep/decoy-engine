"""engine-v2 S12 strategy substrate parity: pandas adapter vs polars adapter.

The migration gate (substrate-decision doc): for every migrated strategy, the
v2 PANDAS execution adapter and the v2 POLARS execution adapter must produce
semantically-equal `outputs` for the same `(plan, sources)`. This is the v2
EXECUTION-path harness, distinct from the V1 graph-engine pandas-vs-duckdb
harness (`test_relational_ops_parity.py` / `test_source_sink_parity.py`).

Accepted differences (see SEMANTIC_DIFFERENCES.md, v2 section): the comparison is
value-level (`to_pydict()`), so Arrow type-width drift introduced by the
pa -> pl -> pa boundary (string vs large_string, etc.) is accepted as long as the
logical values + null positions match. Each migrated strategy adds a case here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter, PolarsExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    deterministic: bool = False,
    provider: str = "x_nobackend",
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(table: str, columns: tuple[tuple[str, ColumnSeed], ...]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x07" * 8,
            per_table=((table, TableSeed(per_column=columns, per_group=())),),
        )
    )


def assert_frames_semantically_equal(pandas_table: pa.Table, polars_table: pa.Table) -> None:
    """Value-level equality: same columns, same per-column values + null positions.

    Arrow type-width drift (string vs large_string, etc.) is accepted; only the
    logical values are compared (SEMANTIC_DIFFERENCES.md v2 section).
    """
    assert polars_table.column_names == pandas_table.column_names
    assert polars_table.to_pydict() == pandas_table.to_pydict()


def _both(plan: Any, sources: dict[str, pa.Table]) -> tuple[pa.Table, pa.Table]:
    pandas_res = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    polars_res = PolarsExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    (table,) = sources
    return pandas_res.outputs[table], polars_res.outputs[table]


# Each entry: (id, plan, sources). Cheap band migrated in S12; medium/expensive
# bands append here as they migrate.
_SMALL = ["a@b.com", None, "carol@example.org", "", "dave@x.io"]
_NUMS = ["12345", "67890", None, "00001", "99999"]
_MEDIUM = [f"user{i:04d}@mail.test" for i in range(1000)]

_CASES: list[tuple[str, Any, dict[str, pa.Table]]] = [
    (
        "passthrough-small",
        _plan("t", (("c", _col("passthrough")),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "redact-small-with-nulls",
        _plan("t", (("c", _col("redact")),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "redact-custom-value",
        _plan("t", (("c", _col("redact", provider_config=(("redact_with", "X"),))),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "truncate-from-start",
        _plan("t", (("c", _col("truncate", provider_config=(("length", 3),))),)),
        {"t": pa.table({"c": _NUMS})},
    ),
    (
        "truncate-from-end",
        _plan(
            "t",
            (("c", _col("truncate", provider_config=(("length", 2), ("from_end", True)))),),
        ),
        {"t": pa.table({"c": _NUMS})},
    ),
    (
        "shuffle-deterministic-small",
        _plan("t", (("c", _col("shuffle", namespace="s_ns", deterministic=True)),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "shuffle-deterministic-medium",
        _plan("t", (("c", _col("shuffle", namespace="s_ns", deterministic=True)),)),
        {"t": pa.table({"c": _MEDIUM})},
    ),
    (
        "redact-medium",
        _plan("t", (("c", _col("redact")),)),
        {"t": pa.table({"c": _MEDIUM})},
    ),
]


@pytest.mark.parametrize("plan,sources", [(c[1], c[2]) for c in _CASES], ids=[c[0] for c in _CASES])
def test_strategy_parity(plan: Any, sources: dict[str, pa.Table]) -> None:
    pandas_out, polars_out = _both(plan, sources)
    assert_frames_semantically_equal(pandas_out, polars_out)

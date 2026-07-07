"""Out-of-core parent key relation tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter, ParquetTransactionalSink
from decoy_engine.execution._fk_keys import fk_join_key_tuple
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core import _runner as _out_of_core_runner
from decoy_engine.execution.out_of_core import (
    build_parent_key_relation,
    check_out_of_core_compatibility,
    mask_child_fk_fail,
    run_fk_out_of_core,
)
from decoy_engine.kernel import hash_array
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_SEED = b"\x00" * 8
_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())


def _hash_col(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _strategy_col(
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


def _when_col(expr: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(("redact_with", "X"),),
        coherent_with=(),
        when=expr,
    )


def _plan() -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
                ),
                (
                    "orders",
                    TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
                ),
            ),
        )
    )


def _plan_from_columns(columns: dict[str, list[tuple[str, str]]]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=tuple(
                (
                    table,
                    TableSeed(
                        per_column=tuple(
                            (column, _hash_col(namespace)) for column, namespace in cols
                        ),
                        per_group=(),
                    ),
                )
                for table, cols in columns.items()
            ),
        )
    )


def _edge(policy: OrphanPolicy = OrphanPolicy.FAIL) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=policy,
    )


def _rel(
    parent_table: str,
    parent_column: str,
    child_table: str,
    child_column: str,
    namespace: str,
    policy: OrphanPolicy = OrphanPolicy.FAIL,
) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent_table,
        parent_columns=(parent_column,),
        child_table=child_table,
        child_columns=(child_column,),
        namespace=namespace,
        orphan_policy=policy,
    )


def _rel_cols(
    parent_table: str,
    parent_columns: tuple[str, ...],
    child_table: str,
    child_columns: tuple[str, ...],
    namespace: str,
    policy: OrphanPolicy = OrphanPolicy.FAIL,
) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent_table,
        parent_columns=parent_columns,
        child_table=child_table,
        child_columns=child_columns,
        namespace=namespace,
        orphan_policy=policy,
    )


def test_parent_key_relation_projects_masked_unique_non_null_keys(tmp_path) -> None:
    parent = pa.table(
        {
            "customer_id": ["c1", "c2", "c1", None],
            "payload": ["wide-a", "wide-b", "wide-c", "wide-null"],
        }
    )

    relation = build_parent_key_relation(
        plan=_plan(),
        parent=parent,
        edge=_edge(),
        temp_dir=tmp_path,
    )

    out = pq.read_table(relation.path)
    assert out.column_names == ["__decoy_fk_join_key", "__decoy_masked_key"]
    rows = {
        key: masked
        for key, masked in zip(
            out.column("__decoy_fk_join_key").to_pylist(),
            out.column("__decoy_masked_key").to_pylist(),
            strict=True,
        )
    }
    expected_masked = hash_array(
        pa.array(["c1", "c2"], from_pandas=True),
        seed=_SEED,
        namespace="cust",
    ).to_pylist()
    assert rows == {
        fk_join_key_tuple(("c1",)): expected_masked[0],
        fk_join_key_tuple(("c2",)): expected_masked[1],
    }
    assert "payload" not in out.column_names


def test_child_fail_join_matches_pandas_fk_output(tmp_path) -> None:
    plan = _plan()
    edge = _edge()
    parent = pa.table({"customer_id": ["c1", "c2"], "name": ["A", "B"]})
    child = pa.table({"customer_id": ["c2", None, "c1"], "amount": [20, 0, 10]})
    relation = build_parent_key_relation(plan=plan, parent=parent, edge=edge, temp_dir=tmp_path)

    out = mask_child_fk_fail(
        child=child,
        edge=edge,
        parent_relation=relation,
        temp_dir=tmp_path,
    )

    graph = RelationshipGraph(edges=(edge,), ordering=())
    pandas = PandasExecutionAdapter().run(
        plan,
        {"customers": parent, "orders": child},
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )
    assert out.to_pydict() == pandas.outputs["orders"].to_pydict()


def test_child_fail_join_raises_on_orphan_before_returning_output(tmp_path) -> None:
    plan = _plan()
    edge = _edge()
    parent = pa.table({"customer_id": ["c1"]})
    child = pa.table({"customer_id": ["c1", "missing"], "amount": [10, 99]})
    relation = build_parent_key_relation(plan=plan, parent=parent, edge=edge, temp_dir=tmp_path)

    with pytest.raises(ExecutionError) as exc:
        mask_child_fk_fail(
            child=child,
            edge=edge,
            parent_relation=relation,
            temp_dir=tmp_path,
        )
    assert getattr(exc.value, "code", None) == "orphan_fk_violation"


def test_run_fk_out_of_core_matches_pandas_outputs(tmp_path) -> None:
    plan = _plan()
    edge = _edge()
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"], "name": ["A", "B"]}),
        "orders": pa.table({"customer_id": ["c2", None, "c1"], "amount": [20, 0, 10]}),
    }

    out = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    assert out.outputs["customers"].to_pydict() == pandas.outputs["customers"].to_pydict()
    assert out.outputs["orders"].to_pydict() == pandas.outputs["orders"].to_pydict()


def test_run_fk_out_of_core_transactional_sink_publishes_on_success(tmp_path) -> None:
    plan = _plan()
    edge = _edge()
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1"]}),
        "orders": pa.table({"customer_id": ["c1"]}),
    }
    target = tmp_path / "published"

    res = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work",
    )

    assert res.outputs == {}
    assert (target / "customers.parquet").exists()
    assert (target / "orders.parquet").exists()


def test_run_fk_out_of_core_transactional_sink_aborts_on_orphan(tmp_path) -> None:
    plan = _plan()
    edge = _edge()
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1"]}),
        "orders": pa.table({"customer_id": ["missing"]}),
    }
    target = tmp_path / "published"

    with pytest.raises(ExecutionError) as exc:
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=ParquetTransactionalSink(target),
            temp_dir=tmp_path / "work",
        )

    assert exc.value.code == "orphan_fk_violation"
    assert not target.exists()


def _chain_plan_graph_sources() -> tuple[Any, RelationshipGraph, dict[str, pa.Table]]:
    plan = _plan_from_columns(
        {
            "customers": [("customer_id", "cust")],
            "orders": [("customer_id", "cust"), ("order_id", "order")],
            "line_items": [("order_id", "order")],
        }
    )
    graph = RelationshipGraph(
        edges=(
            _rel("customers", "customer_id", "orders", "customer_id", "cust"),
            _rel("orders", "order_id", "line_items", "order_id", "order"),
        ),
        ordering=(),
    )
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c2", "c1"], "order_id": ["o2", "o1"]}),
        "line_items": pa.table({"order_id": ["o1", "o2"], "sku": ["a", "b"]}),
    }
    return plan, graph, sources


def test_run_fk_out_of_core_sink_writes_interleaved_in_topo_order(tmp_path, monkeypatch) -> None:
    # A sink that stages tables only after the whole graph has been masked
    # (batch-after-loop) is not memory-bounded: every masked table sits in
    # memory for the duration of the run. This proves staging is interleaved
    # with masking instead, by recording both the mask and staging calls on
    # a shared timeline and asserting each table's staging follows its own
    # mask immediately, rather than all staging clustering after every mask.
    plan, graph, sources = _chain_plan_graph_sources()
    events: list[str] = []
    original_mask_batch = _out_of_core_runner.mask_batch

    def recording_mask_batch(*args: Any, **kwargs: Any) -> pa.RecordBatch:
        events.append(f"mask:{args[1]}")
        return original_mask_batch(*args, **kwargs)

    monkeypatch.setattr(_out_of_core_runner, "mask_batch", recording_mask_batch)

    class RecordingSink:
        def write(self, table: str, data: pa.Table) -> None:
            events.append(f"write:{table}")

        def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
            # Consume the stream (masking happens per pulled batch), then
            # record the completed staging of this table.
            for _batch in batches:
                pass
            events.append(f"write:{table}")

        def commit(self) -> None:
            events.append("commit")

        def abort(self) -> None:
            events.append("abort")

    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=RecordingSink(),
        temp_dir=tmp_path / "work",
    )

    assert events == [
        "mask:customers",
        "write:customers",
        "mask:orders",
        "write:orders",
        "mask:line_items",
        "write:line_items",
        "commit",
    ]


def test_run_fk_out_of_core_sink_output_matches_in_memory_output(tmp_path) -> None:
    plan, graph, sources = _chain_plan_graph_sources()
    target = tmp_path / "published"

    in_memory = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work-mem",
    )
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )

    for table_name in ("customers", "orders", "line_items"):
        published = pq.read_table(target / f"{table_name}.parquet")
        assert published.to_pydict() == in_memory.outputs[table_name].to_pydict()


def test_run_fk_out_of_core_sink_aborts_leaves_nothing_after_upstream_staged(tmp_path) -> None:
    plan = _plan_from_columns(
        {
            "customers": [("customer_id", "cust")],
            "orders": [("customer_id", "cust"), ("order_id", "order")],
            "line_items": [("order_id", "order")],
        }
    )
    graph = RelationshipGraph(
        edges=(
            _rel("customers", "customer_id", "orders", "customer_id", "cust"),
            _rel("orders", "order_id", "line_items", "order_id", "order", OrphanPolicy.FAIL),
        ),
        ordering=(),
    )
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c2", "c1"], "order_id": ["o2", "o1"]}),
        "line_items": pa.table({"order_id": ["missing"], "sku": ["z"]}),
    }
    target = tmp_path / "published"

    with pytest.raises(ExecutionError) as exc:
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=ParquetTransactionalSink(target),
            temp_dir=tmp_path / "work",
        )

    assert exc.value.code == "orphan_fk_violation"
    assert not target.exists()


@pytest.mark.parametrize(
    "policy",
    [OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP],
)
def test_run_fk_out_of_core_orphan_policies_match_pandas(tmp_path, policy: OrphanPolicy) -> None:
    plan = _plan()
    edge = _edge(policy)
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c2", "missing", None, "c1"]}),
    }

    out = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    assert out.outputs["orders"].to_pydict() == pandas.outputs["orders"].to_pydict()
    assert out.outputs["customers"].to_pydict() == pandas.outputs["customers"].to_pydict()
    if policy is OrphanPolicy.WARN:
        assert len(out.warnings) == 1
        assert out.warnings[0].code == "orphan_fk"
        assert out.warnings[0].detail["orphan_rows"] == 1
    else:
        assert out.warnings == ()


def test_run_fk_out_of_core_chain_matches_pandas(tmp_path) -> None:
    plan = _plan_from_columns(
        {
            "customers": [("customer_id", "cust")],
            "orders": [("customer_id", "cust"), ("order_id", "order")],
            "line_items": [("order_id", "order")],
        }
    )
    graph = RelationshipGraph(
        edges=(
            _rel("customers", "customer_id", "orders", "customer_id", "cust"),
            _rel("orders", "order_id", "line_items", "order_id", "order"),
        ),
        ordering=(),
    )
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c2", "c1"], "order_id": ["o2", "o1"]}),
        "line_items": pa.table({"order_id": ["o1", "o2", None], "sku": ["a", "b", "n"]}),
    }

    out = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    assert {name: table.to_pydict() for name, table in out.outputs.items()} == {
        name: table.to_pydict() for name, table in pandas.outputs.items()
    }


def test_run_fk_out_of_core_fanout_matches_pandas(tmp_path) -> None:
    plan = _plan_from_columns(
        {
            "customers": [("customer_id", "cust")],
            "orders": [("customer_id", "cust")],
            "payments": [("customer_id", "cust")],
        }
    )
    graph = RelationshipGraph(
        edges=(
            _rel("customers", "customer_id", "orders", "customer_id", "cust"),
            _rel("customers", "customer_id", "payments", "customer_id", "cust"),
        ),
        ordering=(),
    )
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c2", "c1"]}),
        "payments": pa.table({"customer_id": ["c1", None, "c2"]}),
    }

    out = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    assert {name: table.to_pydict() for name, table in out.outputs.items()} == {
        name: table.to_pydict() for name, table in pandas.outputs.items()
    }


def test_run_fk_out_of_core_sink_fanout_output_matches_in_memory(tmp_path) -> None:
    # Eviction is sharpest on a fanout: the single parent is staged and dropped
    # after both outgoing relations are built, then each child runs off the
    # on-disk relation. Prove the sink-published rows still match the no-sink run.
    plan = _plan_from_columns(
        {
            "customers": [("customer_id", "cust")],
            "orders": [("customer_id", "cust")],
            "payments": [("customer_id", "cust")],
        }
    )
    graph = RelationshipGraph(
        edges=(
            _rel("customers", "customer_id", "orders", "customer_id", "cust"),
            _rel("customers", "customer_id", "payments", "customer_id", "cust"),
        ),
        ordering=(),
    )
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c2", "c1"]}),
        "payments": pa.table({"customer_id": ["c1", None, "c2"]}),
    }
    target = tmp_path / "published"

    in_memory = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work-mem",
    )
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )

    for table_name in ("customers", "orders", "payments"):
        published = pq.read_table(target / f"{table_name}.parquet")
        assert published.to_pydict() == in_memory.outputs[table_name].to_pydict()


def test_run_fk_out_of_core_distinct_multi_parent_child_matches_pandas(tmp_path) -> None:
    plan = _plan_from_columns(
        {
            "customers": [("customer_id", "cust")],
            "regions": [("region_id", "region")],
            "orders": [("customer_id", "cust"), ("region_id", "region")],
        }
    )
    graph = RelationshipGraph(
        edges=(
            _rel("customers", "customer_id", "orders", "customer_id", "cust"),
            _rel("regions", "region_id", "orders", "region_id", "region"),
        ),
        ordering=(),
    )
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "regions": pa.table({"region_id": ["r1", "r2"]}),
        "orders": pa.table({"customer_id": ["c2", "c1"], "region_id": ["r1", "r2"]}),
    }

    out = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    assert {name: table.to_pydict() for name, table in out.outputs.items()} == {
        name: table.to_pydict() for name, table in pandas.outputs.items()
    }


@pytest.mark.parametrize(
    "policy",
    [OrphanPolicy.FAIL, OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP],
)
def test_run_fk_out_of_core_composite_fk_matches_pandas(tmp_path, policy: OrphanPolicy) -> None:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "accounts",
                    TableSeed(
                        per_column=(
                            ("country", _hash_col("country")),
                            ("account_id", _hash_col("acct")),
                        ),
                        per_group=(),
                    ),
                ),
                (
                    "transactions",
                    TableSeed(
                        per_column=(),
                        per_group=(
                            (
                                "account_id__country",
                                GroupSeed(
                                    namespace="acct_rel",
                                    coherent_columns=("country", "account_id"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            _rel_cols(
                "accounts",
                ("country", "account_id"),
                "transactions",
                ("country", "account_id"),
                "acct_rel",
                policy,
            ),
        ),
        ordering=(),
    )
    child_accounts = ["a2", "a1", None] if policy is OrphanPolicy.FAIL else ["a2", "missing", None]
    sources = {
        "accounts": pa.table({"country": ["US", "CA"], "account_id": ["a1", "a2"]}),
        "transactions": pa.table(
            {
                "country": ["CA", "US", None],
                "account_id": child_accounts,
                "amount": [20, 10, 0],
            }
        ),
    }

    out = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    assert {name: table.to_pydict() for name, table in out.outputs.items()} == {
        name: table.to_pydict() for name, table in pandas.outputs.items()
    }
    if policy is OrphanPolicy.WARN:
        assert len(out.warnings) == 1


@pytest.mark.parametrize(
    ("strategy", "provider_config"),
    [
        ("redact", (("redact_with", "X"),)),
        ("truncate", (("length", 2),)),
        ("passthrough", ()),
    ],
)
@pytest.mark.parametrize("policy", [OrphanPolicy.FAIL, OrphanPolicy.REMAP])
def test_run_fk_out_of_core_namespace_agnostic_parent_strategies_match_pandas(
    tmp_path,
    strategy: str,
    provider_config: tuple[tuple[str, Any], ...],
    policy: OrphanPolicy,
) -> None:
    seed = _strategy_col(strategy, provider_config=provider_config)
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("parents", TableSeed(per_column=(("id", seed),), per_group=())),
                ("children", TableSeed(per_column=(("parent_id", seed),), per_group=())),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(_rel("parents", "id", "children", "parent_id", "parent_rel", policy),),
        ordering=(),
    )
    child_ids = ["CD456", "AB123"] if policy is OrphanPolicy.FAIL else ["CD456", "missing"]
    sources = {
        "parents": pa.table({"id": ["AB123", "CD456"]}),
        "children": pa.table({"parent_id": child_ids}),
    }

    out = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    assert {name: table.to_pydict() for name, table in out.outputs.items()} == {
        name: table.to_pydict() for name, table in pandas.outputs.items()
    }


def _plan_with_when_gated_column() -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
                ),
                (
                    "orders",
                    TableSeed(
                        per_column=(
                            ("customer_id", _hash_col("cust")),
                            ("status", _when_col("amount > 0")),
                        ),
                        per_group=(),
                    ),
                ),
            ),
        )
    )


def test_check_out_of_core_compatibility_rejects_when_predicate() -> None:
    plan = _plan_with_when_gated_column()
    edge = _edge()
    graph = RelationshipGraph(edges=(edge,), ordering=())
    work = order_work(build_work_list(plan, _REG), graph)

    result = check_out_of_core_compatibility(plan, work, graph)

    assert not result.accepted
    assert "out_of_core_when_predicate_unsupported" in {r.code for r in result.rejections}


def test_run_fk_out_of_core_rejects_when_predicate(tmp_path) -> None:
    plan = _plan_with_when_gated_column()
    edge = _edge()
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1"]}),
        "orders": pa.table({"customer_id": ["c1"], "status": ["active"], "amount": [1]}),
    }

    with pytest.raises(ExecutionError) as exc:
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            temp_dir=tmp_path / "work",
        )

    assert exc.value.code == "out_of_core_when_predicate_unsupported"


def test_check_out_of_core_compatibility_rejects_uncovered_composite_group() -> None:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "accounts",
                    TableSeed(per_column=(("country", _hash_col("country_ns")),), per_group=()),
                ),
                (
                    "transactions",
                    TableSeed(
                        per_column=(),
                        per_group=(
                            (
                                "account_id__country",
                                GroupSeed(
                                    namespace="acct_rel",
                                    coherent_columns=("country", "account_id"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
    )
    # Edge only covers "country"; "account_id" is not a child FK column of any
    # edge for this table, so the group is not provably join-covered.
    edge = _rel("accounts", "country", "transactions", "country", "country_ns")
    graph = RelationshipGraph(edges=(edge,), ordering=())
    work = order_work(build_work_list(plan, _REG), graph)

    result = check_out_of_core_compatibility(plan, work, graph)

    assert not result.accepted
    assert "out_of_core_composite_group_uncovered" in {r.code for r in result.rejections}


def test_check_out_of_core_compatibility_accepts_fully_covered_composite_group() -> None:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "accounts",
                    TableSeed(
                        per_column=(
                            ("country", _hash_col("country")),
                            ("account_id", _hash_col("acct")),
                        ),
                        per_group=(),
                    ),
                ),
                (
                    "transactions",
                    TableSeed(
                        per_column=(),
                        per_group=(
                            (
                                "account_id__country",
                                GroupSeed(
                                    namespace="acct_rel",
                                    coherent_columns=("country", "account_id"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
    )
    edge = _rel_cols(
        "accounts", ("country", "account_id"), "transactions", ("country", "account_id"), "acct_rel"
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    work = order_work(build_work_list(plan, _REG), graph)

    result = check_out_of_core_compatibility(plan, work, graph)

    assert result.accepted


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN])
def test_run_fk_out_of_core_orphan_preserves_normalized_float_key(
    tmp_path, policy: OrphanPolicy
) -> None:
    """A whole-number float64 orphan key must come out as the fk_key_value-
    normalized int (matching the pandas oracle), not the raw float. Both rows
    are orphans (no row in `customers` matches) and neither is null, so the
    column stays homogeneous numeric end to end and the int/float distinction
    survives the pandas + pyarrow round trip instead of being coerced away.

    Parent strategy is passthrough (not hash): derive-input canonicalization
    rejects floats outright (separate, intentional restriction; see
    _fk_keys.py module docstring), so a float64 FK key can only flow through
    a parent strategy that never canonicalizes the value.
    """
    seed = _strategy_col("passthrough")
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("customers", TableSeed(per_column=(("customer_id", seed),), per_group=())),
                ("orders", TableSeed(per_column=(("customer_id", seed),), per_group=())),
            ),
        )
    )
    edge = _rel("customers", "customer_id", "orders", "customer_id", "cust", policy)
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"customer_id": pa.array([99.0], type=pa.float64())}),
        "orders": pa.table({"customer_id": pa.array([5.0, 7.0], type=pa.float64())}),
    }

    out = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    out_col = out.outputs["orders"].column("customer_id")
    pandas_col = pandas.outputs["orders"].column("customer_id")
    assert out_col.type == pandas_col.type
    out_vals = out_col.to_pylist()
    pandas_vals = pandas_col.to_pylist()
    for ooc_val, oracle_val in zip(out_vals, pandas_vals, strict=True):
        assert type(ooc_val) is type(oracle_val) and ooc_val == oracle_val
    assert pandas_vals == [5, 7]


def test_run_fk_out_of_core_wipes_staging_subtrees_for_caller_temp_dir(tmp_path) -> None:
    plan = _plan()
    edge = _edge()
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1"]}),
        "orders": pa.table({"customer_id": ["c1"]}),
    }
    work_dir = tmp_path / "caller-owned"

    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=work_dir,
    )

    assert not (work_dir / "relations").exists()
    assert not (work_dir / "joins").exists()
    assert work_dir.exists()

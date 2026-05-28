"""engine-v2 S9 slice 1: work-list construction + execution ordering.

Covers the BLOCKER Dennis caught at spec review (the work list comes from the
seed envelope, not FK-only plan.ordering) and the R17 composite-before-FK-child
ordering.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import WorkNode, build_work_list, order_work
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import (
    OrphanPolicy,
    RelationshipEdge,
    RelationshipGraph,
)

_REG = get_default_registry()


def _col(provider: str, *, coherent_with: tuple[str, ...] = ()) -> ColumnSeed:
    return ColumnSeed(
        namespace="ns",
        strategy="faker",
        provider=provider,
        backend_type="faker",
        backend_version="x",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(),
        coherent_with=tuple(coherent_with),
    )


def _plan(per_table: list[tuple[str, TableSeed]]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(job_seed=b"\x00" * 8, per_table=tuple(per_table))
    )


def _node(table: str, columns: tuple[str, ...], kind: str = "scalar") -> WorkNode:
    return WorkNode(
        table=table,
        columns=tuple(columns),
        kind=kind,
        strategy="s",
        provider="p",
        plan_slice=_col("person_email"),
    )


def _edge(pt: str, pc: tuple[str, ...], ct: str, cc: tuple[str, ...]) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=pt,
        parent_columns=tuple(pc),
        child_table=ct,
        child_columns=tuple(cc),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def _graph(*edges: RelationshipEdge) -> RelationshipGraph:
    return RelationshipGraph(edges=tuple(edges), ordering=())


class TestBuildWorkList:
    def test_no_fk_single_table_masks_all_columns(self) -> None:
        # H1 regression guard: plan.ordering would be empty (no FK); the work
        # list MUST still cover every maskable column.
        ts = TableSeed(
            per_column=(("email", _col("person_email")), ("name", _col("person_name"))),
            per_group=(),
        )
        work = build_work_list(_plan([("people", ts)]), _REG)
        assert {w.columns for w in work} == {("email",), ("name",)}
        assert all(w.kind == "scalar" for w in work)

    def test_composite_columns_collapse_to_one_node(self) -> None:
        ts = TableSeed(
            per_column=(
                ("first_name", _col("composite_name_email", coherent_with=("last_name", "email"))),
                ("last_name", _col("composite_name_email", coherent_with=("first_name", "email"))),
                ("email", _col("composite_name_email", coherent_with=("first_name", "last_name"))),
            ),
            per_group=(),
        )
        work = build_work_list(_plan([("people", ts)]), _REG)
        assert len(work) == 1
        assert work[0].kind == "composite"
        assert work[0].columns == tuple(sorted(("first_name", "last_name", "email")))

    def test_per_group_becomes_composite_fk_group_node(self) -> None:
        gs = GroupSeed(namespace="g_ns", coherent_columns=("a", "b"))
        ts = TableSeed(per_column=(), per_group=(("a__b", gs),))
        work = build_work_list(_plan([("t", ts)]), _REG)
        assert len(work) == 1
        assert work[0].kind == "composite_fk_group"
        assert work[0].columns == ("a", "b")

    def test_mixed_scalar_and_composite(self) -> None:
        ts = TableSeed(
            per_column=(
                ("email_addr", _col("person_email")),
                ("first_name", _col("composite_name_email", coherent_with=("last_name", "email"))),
                ("last_name", _col("composite_name_email", coherent_with=("first_name", "email"))),
                ("email", _col("composite_name_email", coherent_with=("first_name", "last_name"))),
            ),
            per_group=(),
        )
        work = build_work_list(_plan([("people", ts)]), _REG)
        assert sorted(w.kind for w in work) == ["composite", "scalar"]


class TestOrderWork:
    def test_fk_parent_before_child(self) -> None:
        parent = _node("customers", ("id",))
        child = _node("orders", ("customer_id",))
        edge = _edge("customers", ("id",), "orders", ("customer_id",))
        # child passed first to prove the sort reorders by the dependency.
        ordered = order_work([child, parent], _graph(edge))
        keys = [w.key for w in ordered]
        assert keys.index(parent.key) < keys.index(child.key)

    def test_r17_composite_before_fk_child(self) -> None:
        comp = _node(
            "people", tuple(sorted(("first_name", "last_name", "email"))), kind="composite"
        )
        child = _node("contacts", ("person_first",))
        edge = _edge("people", ("first_name",), "contacts", ("person_first",))
        ordered = order_work([child, comp], _graph(edge))
        keys = [w.key for w in ordered]
        assert keys.index(comp.key) < keys.index(child.key)

    def test_independent_nodes_sorted_deterministic(self) -> None:
        a = _node("t", ("z",))
        b = _node("t", ("a",))
        c = _node("s", ("m",))
        ordered = order_work([a, b, c], _graph())
        assert [w.key for w in ordered] == [("s", ("m",)), ("t", ("a",)), ("t", ("z",))]

    def test_cycle_raises(self) -> None:
        a = _node("ta", ("x",))
        b = _node("tb", ("y",))
        e1 = _edge("ta", ("x",), "tb", ("y",))  # b waits on a
        e2 = _edge("tb", ("y",), "ta", ("x",))  # a waits on b
        with pytest.raises(ExecutionError) as exc:
            order_work([a, b], _graph(e1, e2))
        assert exc.value.code == "cyclic_work_ordering"

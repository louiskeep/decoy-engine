"""SS3 closure tests. Includes acceptance test 2 (cycle termination via no-growth, not a timeout)."""

from __future__ import annotations

import polars as pl
import pytest

from decoy_engine.subset._closure import (
    _downward_step,
    _upward_step,
    compute_closure,
    verify_closure,
)
from decoy_engine.subset._errors import SubsetInternalError
from decoy_engine.subset._keys import RI
from decoy_engine.subset._types import SubsetEdge

NOOP_BUDGET = lambda survivors, edge_stats: None


def _kf(**cols) -> pl.DataFrame:
    return pl.DataFrame(cols).with_row_index(RI)


def _edge(pt, pc, ct, cc, ns=None) -> SubsetEdge:
    return SubsetEdge(
        edge_id=f"{pt}.{','.join(pc)} -> {ct}.{','.join(cc)}",
        parent_table=pt,
        parent_columns=pc,
        child_table=ct,
        child_columns=cc,
        orphan_policy="preserve",
        namespace=ns,
    )


def test_chain_fixture_exact_survivors_and_fixpoint() -> None:
    customers = _kf(id=[1, 2, 3])
    orders = _kf(id=[10, 11, 12], customer_id=[1, 2, 3])
    order_items = _kf(id=[100, 101, 102], order_id=[10, 11, 12])
    key_frames = {"customers": customers, "orders": orders, "order_items": order_items}
    edges = (
        _edge("customers", ("id",), "orders", ("customer_id",)),
        _edge("orders", ("id",), "order_items", ("order_id",)),
    )
    directions = {e.edge_id: "both" for e in edges}
    seed_rows = {"customers": frozenset({0})}  # customer id=1

    result = compute_closure(
        edges=edges,
        directions=directions,
        key_frames=key_frames,
        seed_rows=seed_rows,
        budget_check=NOOP_BUDGET,
    )
    assert result.survivors["customers"] == frozenset({0})
    assert result.survivors["orders"] == frozenset({0})
    assert result.survivors["order_items"] == frozenset({0})
    assert result.terminated_by == "fixpoint"
    assert result.trace[-1].rows_added == 0
    assert result.rounds == 2  # round 1 adds orders+items, round 2 adds nothing


def test_self_reference_terminates_via_no_growth() -> None:
    # employee(id, manager_id); rows: (1,2) (2,1) (3,2) (4,3) (5,None) (6,5)
    employee = _kf(
        id=[1, 2, 3, 4, 5, 6],
        manager_id=[2, 1, 2, 3, None, 5],
    )
    edges = (_edge("employee", ("id",), "employee", ("manager_id",)),)
    directions = {edges[0].edge_id: "both"}
    key_frames = {"employee": employee}
    seed_rows = {"employee": frozenset({3})}  # row index 3 == id 4

    result = compute_closure(
        edges=edges,
        directions=directions,
        key_frames=key_frames,
        seed_rows=seed_rows,
        budget_check=NOOP_BUDGET,
    )
    surviving_ids = set(
        employee.filter(pl.col(RI).is_in(list(result.survivors["employee"])))["id"].to_list()
    )
    assert surviving_ids == {1, 2, 3, 4}
    # Hand-computed: round1 pulls id3 (manager of id4); round2 pulls id2 (manager
    # of id3); round3 pulls id1 (manager of id2, discovered via downward from the
    # newly-upward-pulled id2/id3/id4 set); round4 adds nothing. rounds == 4.
    assert result.rounds == 4

    # Monotone trace: cumulative survivor count never shrinks round over round.
    cumulative = 0
    for rt in result.trace:
        assert rt.rows_added >= 0
        cumulative += rt.rows_added
    assert result.trace[-1].rows_added == 0
    assert result.trace[-2].rows_added > 0

    # No-growth exercised, not a timeout: re-running the step functions on the
    # final survivor state must add nothing (a genuine fixpoint).
    survivors_mut = {t: set(s) for t, s in result.survivors.items()}
    assert _upward_step(edges[0], survivors_mut, key_frames) == set()
    assert _downward_step(edges[0], survivors_mut, key_frames) == set()

    verify_closure(edges=edges, directions=directions, key_frames=key_frames, result=result)


def test_mutual_cycle_terminates_and_pins_exact_sets_no_over_pull() -> None:
    # a(id, b_ref): a1->b1, a2->b2 (tail); b(id, a_ref): b1->a1 (closes cycle), b2->a3 (tail)
    a = _kf(id=["a1", "a2", "a3"], b_ref=["b1", "b2", None])
    b = _kf(id=["b1", "b2"], a_ref=["a1", "a3"])
    edges = (
        _edge("a", ("id",), "b", ("a_ref",)),
        _edge("b", ("id",), "a", ("b_ref",)),
    )
    directions = {e.edge_id: "both" for e in edges}
    key_frames = {"a": a, "b": b}
    seed_rows = {"a": frozenset({0})}  # a1

    result = compute_closure(
        edges=edges,
        directions=directions,
        key_frames=key_frames,
        seed_rows=seed_rows,
        budget_check=NOOP_BUDGET,
    )
    surviving_a = set(a.filter(pl.col(RI).is_in(list(result.survivors["a"])))["id"].to_list())
    surviving_b = set(b.filter(pl.col(RI).is_in(list(result.survivors["b"])))["id"].to_list())
    assert surviving_a == {"a1"}
    assert surviving_b == {"b1"}  # the tail (a2/b2/a3) is NOT pulled: no over-pull.

    assert result.trace[-1].rows_added == 0
    assert result.trace[-2].rows_added > 0

    # Determinism: rerun produces an identical ClosureResult.
    result2 = compute_closure(
        edges=edges,
        directions=directions,
        key_frames=key_frames,
        seed_rows=seed_rows,
        budget_check=NOOP_BUDGET,
    )
    assert result.survivors == result2.survivors
    assert result.rounds == result2.rounds
    assert result.trace == result2.trace
    assert result.edge_stats == result2.edge_stats


def test_mutual_cycle_tail_pulled_only_when_seeded_from_it() -> None:
    a = _kf(id=["a1", "a2", "a3"], b_ref=["b1", "b2", None])
    b = _kf(id=["b1", "b2"], a_ref=["a1", "a3"])
    edges = (
        _edge("a", ("id",), "b", ("a_ref",)),
        _edge("b", ("id",), "a", ("b_ref",)),
    )
    directions = {e.edge_id: "both" for e in edges}
    key_frames = {"a": a, "b": b}
    seed_rows = {"a": frozenset({1})}  # a2

    result = compute_closure(
        edges=edges,
        directions=directions,
        key_frames=key_frames,
        seed_rows=seed_rows,
        budget_check=NOOP_BUDGET,
    )
    surviving_a = set(a.filter(pl.col(RI).is_in(list(result.survivors["a"])))["id"].to_list())
    surviving_b = set(b.filter(pl.col(RI).is_in(list(result.survivors["b"])))["id"].to_list())
    assert surviving_a == {"a2", "a3"}  # a2 -> b2 (downward) -> b2.a_ref=a3 (upward)
    assert surviving_b == {"b2"}


def test_null_fk_survives_via_other_parent_but_no_upward_pull() -> None:
    p1 = _kf(id=[1])
    p2 = _kf(id=[9])
    c = pl.DataFrame(
        {"id": [100], "fk1": [1], "fk2": [None]},
        schema={"id": pl.Int64, "fk1": pl.Int64, "fk2": pl.Int64},
    ).with_row_index(RI)
    edges = (
        _edge("p1", ("id",), "c", ("fk1",)),
        _edge("p2", ("id",), "c", ("fk2",)),
    )
    directions = {e.edge_id: "both" for e in edges}
    key_frames = {"p1": p1, "p2": p2, "c": c}
    seed_rows = {"c": frozenset({0})}

    result = compute_closure(
        edges=edges,
        directions=directions,
        key_frames=key_frames,
        seed_rows=seed_rows,
        budget_check=NOOP_BUDGET,
    )
    assert result.survivors["c"] == frozenset({0})
    assert result.survivors["p1"] == frozenset({0})  # pulled via fk1
    assert result.survivors["p2"] == frozenset()  # null fk2: no upward pull


def test_direction_toggles() -> None:
    parent = _kf(id=[1, 2])
    child = _kf(id=[10, 11], parent_id=[1, 2])
    edge = _edge("parent", ("id",), "child", ("parent_id",))
    key_frames = {"parent": parent, "child": child}

    # upward-only: never adds children.
    result = compute_closure(
        edges=(edge,),
        directions={edge.edge_id: "upward"},
        key_frames=key_frames,
        seed_rows={"parent": frozenset({0})},
        budget_check=NOOP_BUDGET,
    )
    assert result.survivors["child"] == frozenset()

    # downward-only: never adds parents.
    result = compute_closure(
        edges=(edge,),
        directions={edge.edge_id: "downward"},
        key_frames=key_frames,
        seed_rows={"child": frozenset({0})},
        budget_check=NOOP_BUDGET,
    )
    assert result.survivors["parent"] == frozenset()

    # none: adds nothing.
    result = compute_closure(
        edges=(edge,),
        directions={edge.edge_id: "none"},
        key_frames=key_frames,
        seed_rows={"parent": frozenset({0}), "child": frozenset()},
        budget_check=NOOP_BUDGET,
    )
    assert result.survivors["child"] == frozenset()
    assert result.survivors["parent"] == frozenset({0})


def test_multi_parent_upward_adds_matching_rows_in_both_parents() -> None:
    p1 = _kf(id=[5])
    p2 = _kf(id=[5])
    c = _kf(id=[1], k=[5])
    edges = (
        _edge("p1", ("id",), "c", ("k",)),
        _edge("p2", ("id",), "c", ("k",)),
    )
    directions = {e.edge_id: "both" for e in edges}
    key_frames = {"p1": p1, "p2": p2, "c": c}
    seed_rows = {"c": frozenset({0})}

    result = compute_closure(
        edges=edges,
        directions=directions,
        key_frames=key_frames,
        seed_rows=seed_rows,
        budget_check=NOOP_BUDGET,
    )
    assert result.survivors["p1"] == frozenset({0})
    assert result.survivors["p2"] == frozenset({0})


def test_verify_closure_detects_corrupted_result() -> None:
    parent = _kf(id=[1])
    child = _kf(id=[10], parent_id=[1])
    edge = _edge("parent", ("id",), "child", ("parent_id",))
    key_frames = {"parent": parent, "child": child}
    directions = {edge.edge_id: "both"}

    result = compute_closure(
        edges=(edge,),
        directions=directions,
        key_frames=key_frames,
        seed_rows={"child": frozenset({0})},
        budget_check=NOOP_BUDGET,
    )
    verify_closure(edges=(edge,), directions=directions, key_frames=key_frames, result=result)

    corrupted = type(result)(
        survivors={"parent": frozenset(), "child": result.survivors["child"]},
        rounds=result.rounds,
        terminated_by=result.terminated_by,
        edge_stats=result.edge_stats,
        trace=result.trace,
    )
    with pytest.raises(SubsetInternalError) as excinfo:
        verify_closure(
            edges=(edge,), directions=directions, key_frames=key_frames, result=corrupted
        )
    assert excinfo.value.code == "subset_closure_invariant_violated"
    assert edge.edge_id in excinfo.value.message

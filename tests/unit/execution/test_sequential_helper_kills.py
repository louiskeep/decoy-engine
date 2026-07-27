"""TQ mutation-kill oracles for `execution/_sequential.py` HELPER functions:
`table_topo_order` (FK-topological table sort, Kahn) and
`_has_transactional_write_contract` (the write/commit/abort sink probe). The big
`run_sequential` orchestrator is graded by its own kill file. Both helpers are
pure enough to drive directly: `table_topo_order` reads only
`plan.seed_envelope.per_table` + `graph.edges`, so a stub plan + a hand-built
`RelationshipGraph` suffice.
"""

from __future__ import annotations

import types

import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution._sequential import (
    _has_transactional_write_contract,
    table_topo_order,
)
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph


def _plan(*names: str) -> object:
    """Stub with just the `seed_envelope.per_table` table order table_topo_order reads."""
    return types.SimpleNamespace(
        seed_envelope=types.SimpleNamespace(per_table=[(n, None) for n in names])
    )


def _edge(parent: str, child: str) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent,
        parent_columns=("k",),
        child_table=child,
        child_columns=("fk",),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def _graph(*edges: RelationshipEdge) -> RelationshipGraph:
    return RelationshipGraph(edges=tuple(edges), ordering=())


class TestTableTopoOrder:
    def test_parent_precedes_child_overriding_seed_order(self) -> None:
        # child is listed FIRST in the seed order; the FK edge must still put the
        # parent before it. Kills the seed-dedup inversion (mut_3), the children
        # dedup-guard inversion (mut_20, which drops the child's indegree so it
        # never waits on the parent), and the seen.add/append(None) mutants.
        order = table_topo_order(_plan("child", "parent"), _graph(_edge("parent", "child")))
        assert order.index("parent") < order.index("child")
        assert set(order) == {"parent", "child"}

    def test_child_with_two_parents_waits_for_both(self) -> None:
        # indegree must ACCUMULATE across two parents (`+= 1`); the `= 1` mutant
        # sets it to 1 so the child is ready after only ONE parent -> child could
        # precede the second parent.
        order = table_topo_order(_plan("c", "p1", "p2"), _graph(_edge("p1", "c"), _edge("p2", "c")))
        assert order.index("p1") < order.index("c")
        assert order.index("p2") < order.index("c")

    def test_ready_tiebreak_follows_seed_position_not_name(self) -> None:
        # Two independent tables (no edges), seed order [z, a] so position(z) <
        # position(a) but name-order is [a, z]. The ready-queue sorts by POSITION;
        # `key=None` would sort by name and flip them.
        assert table_topo_order(_plan("z", "a"), _graph()) == ["z", "a"]

    def test_self_fk_edge_is_skipped_no_false_cycle(self) -> None:
        # A self-FK (a->a) must be skipped for table ordering; the `==`->`!=`
        # mutant treats it as a real edge (a becomes its own child, indegree 1),
        # so `a` never becomes ready and the function wrongly raises a cycle.
        order = table_topo_order(_plan("a", "b"), _graph(_edge("a", "a"), _edge("a", "b")))
        assert order == ["a", "b"]

    def test_self_fk_before_real_edge_still_orders_real_edge(self) -> None:
        # The self-FK `continue` must not `break` the edge loop before the real
        # a->b edge is counted.
        order = table_topo_order(_plan("b", "a"), _graph(_edge("a", "a"), _edge("a", "b")))
        assert order.index("a") < order.index("b")

    def test_edge_only_table_added_once_and_named(self) -> None:
        # `b` appears ONLY in edges (not in the seed order), as the child of two
        # parents. The edge loop must add it by NAME, exactly once. mut_8
        # (`order_seed.append(None)`) drops `b` from the order; mut_7
        # (`seen.add(None)`) fails to mark it seen so the second edge appends it
        # again -> a duplicate.
        order = table_topo_order(_plan("a", "c"), _graph(_edge("a", "b"), _edge("c", "b")))
        assert order.count("b") == 1
        assert None not in order

    def test_cycle_raises_coded_error(self) -> None:
        # a->b->a is an unorderable cycle. Kills code=None / renamed / re-cased,
        # and a nulled message (asserted non-empty).
        with pytest.raises(ExecutionError) as exc:
            table_topo_order(_plan("a", "b"), _graph(_edge("a", "b"), _edge("b", "a")))
        assert exc.value.code == "relationship_cycle"
        assert exc.value.message  # non-empty (kills a nulled message)


def _sink(**methods: object) -> object:
    """A sink stub exposing only the named callable methods."""
    return types.SimpleNamespace(**{name: (lambda *a, **k: None) for name in methods})


class TestHasTransactionalWriteContract:
    def test_full_three_method_sink_is_transactional(self) -> None:
        assert _has_transactional_write_contract(_sink(write=1, commit=1, abort=1)) is True

    def test_missing_abort_is_not_transactional(self) -> None:
        # write+commit but no abort: the real contract needs ALL THREE. Kills the
        # dropped-abort-clause mutant (returns True) and the no-default
        # getattr(..., "abort") mutant (raises instead of returning False).
        assert _has_transactional_write_contract(_sink(write=1, commit=1)) is False

    def test_missing_write_is_not_transactional(self) -> None:
        # commit+abort but no write: kills the dropped-write-clause mutant.
        assert _has_transactional_write_contract(_sink(commit=1, abort=1)) is False

    def test_missing_commit_is_not_transactional(self) -> None:
        # write only (commit absent): the no-default getattr(..., "commit")
        # mutant raises AttributeError instead of returning False.
        assert _has_transactional_write_contract(_sink(write=1)) is False

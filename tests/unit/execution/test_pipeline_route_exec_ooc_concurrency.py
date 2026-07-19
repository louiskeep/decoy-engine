"""FIX 1 concurrency analysis: `_max_concurrent_ooc_instances` sizes
`resolve_ooc_memory_limit`'s `max_concurrent_instances` EXACTLY from a job's
own relationship graph, rather than falling back to `_budget.py`'s
conservative default.

Mirrors `_budget.py`'s module-docstring analysis of `_runner.py`: one table's
worst case is its incoming-edge count plus one (a concurrent outgoing
relation build), and only one table streams at a time, so the run's worst
case is the max of that over every table.
"""

from __future__ import annotations

from decoy_engine.execution._pipeline_route_exec import _max_concurrent_ooc_instances
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph


def _edge(parent: str, child: str) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent,
        parent_columns=("id",),
        child_table=child,
        child_columns=(f"{parent}_id",),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def _graph(*edges: RelationshipEdge) -> RelationshipGraph:
    return RelationshipGraph(edges=tuple(edges), ordering=())


class TestMaxConcurrentOocInstances:
    def test_no_edges_yields_one(self) -> None:
        assert _max_concurrent_ooc_instances(_graph()) == 1

    def test_single_chain_peaks_at_two(self) -> None:
        # parent -> child -> grandchild: "child" has 1 incoming edge (from
        # parent) AND 1 outgoing edge (to grandchild) live at once = 2. This
        # is the real 100M-row cloud benchmark's shape.
        graph = _graph(_edge("parent", "child"), _edge("child", "grandchild"))
        assert _max_concurrent_ooc_instances(graph) == 2

    def test_leaf_table_with_no_outgoing_edge_does_not_add_one(self) -> None:
        # A single edge into a leaf child: 1 incoming joiner, no outgoing
        # relation build concurrent with it.
        graph = _graph(_edge("parent", "child"))
        assert _max_concurrent_ooc_instances(graph) == 1

    def test_wide_fan_in_dominates(self) -> None:
        # One table with 5 incoming edges (from 5 different parents) and no
        # outgoing edge of its own: 5 joiners live at once, no relation build.
        graph = _graph(*(_edge(f"parent{i}", "hub") for i in range(5)))
        assert _max_concurrent_ooc_instances(graph) == 5

    def test_fan_in_plus_outgoing_edge_adds_one(self) -> None:
        # "hub" has 3 incoming edges AND 1 outgoing edge to "leaf": worst
        # case is 3 joiners + 1 concurrent relation build = 4.
        graph = _graph(
            _edge("p0", "hub"),
            _edge("p1", "hub"),
            _edge("p2", "hub"),
            _edge("hub", "leaf"),
        )
        assert _max_concurrent_ooc_instances(graph) == 4

    def test_result_is_the_max_across_tables_not_the_sum(self) -> None:
        # Two independent chains: neither table sees the other's edges, so
        # the run's worst case is the larger single-table peak (2), not the
        # combined edge count (4).
        graph = _graph(
            _edge("p1", "c1"),
            _edge("c1", "g1"),
            _edge("p2", "c2"),
            _edge("c2", "g2"),
        )
        assert _max_concurrent_ooc_instances(graph) == 2

"""Unit tests for decoy_engine.graph.planner.

Sprint 1.1 (graph runtime split): verifies that build_plan() correctly
computes topology, edge indexing, and consumer counts before any op executes.

These tests are intentionally free of execution context or Arrow deps —
planner input/output is plain Python dicts and ints.
"""
from __future__ import annotations

import pytest

from decoy_engine.graph.planner import GraphPlan, build_plan
from decoy_engine.internal.validator import ValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node(id: str, kind: str = "filter") -> dict:
    return {"id": id, "kind": kind, "config": {}}


def _edge(from_: str, to: str) -> dict:
    return {"from": from_, "to": to}


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

class TestReturnType:
    def test_returns_graph_plan(self):
        plan = build_plan([_node("a")], [])
        assert isinstance(plan, GraphPlan)

    def test_empty_graph(self):
        plan = build_plan([], [])
        assert plan.ordered_ids == []
        assert plan.by_id == {}
        assert plan.consumer_counts == {}
        assert plan.in_edges_by_node == {}


# ---------------------------------------------------------------------------
# Topology / ordered_ids
# ---------------------------------------------------------------------------

class TestTopology:
    def test_single_node(self):
        plan = build_plan([_node("only")], [])
        assert plan.ordered_ids == ["only"]

    def test_linear_order(self):
        nodes = [_node("a"), _node("b"), _node("c")]
        edges = [_edge("a", "b"), _edge("b", "c")]
        plan = build_plan(nodes, edges)
        assert plan.ordered_ids == ["a", "b", "c"]

    def test_fan_out_source_first(self):
        """Fan-out: source must come before both consumers."""
        nodes = [_node("src"), _node("t1"), _node("t2")]
        edges = [_edge("src", "t1"), _edge("src", "t2")]
        plan = build_plan(nodes, edges)
        assert plan.ordered_ids[0] == "src"
        assert set(plan.ordered_ids[1:]) == {"t1", "t2"}

    def test_fan_in_both_sources_before_join(self):
        """Fan-in: both sources must precede the join node."""
        nodes = [_node("a"), _node("b"), _node("join")]
        edges = [_edge("a", "join"), _edge("b", "join")]
        plan = build_plan(nodes, edges)
        pos = {nid: i for i, nid in enumerate(plan.ordered_ids)}
        assert pos["a"] < pos["join"]
        assert pos["b"] < pos["join"]

    def test_cycle_raises_validation_error(self):
        nodes = [_node("a"), _node("b")]
        edges = [_edge("a", "b"), _edge("b", "a")]
        with pytest.raises(ValidationError, match="cycle"):
            build_plan(nodes, edges)

    def test_self_loop_raises(self):
        nodes = [_node("a")]
        edges = [_edge("a", "a")]
        with pytest.raises(ValidationError, match="cycle"):
            build_plan(nodes, edges)


# ---------------------------------------------------------------------------
# by_id index
# ---------------------------------------------------------------------------

class TestById:
    def test_maps_id_to_node_dict(self):
        nodes = [_node("x"), _node("y")]
        plan = build_plan(nodes, [])
        assert plan.by_id["x"] is nodes[0]
        assert plan.by_id["y"] is nodes[1]

    def test_all_nodes_present(self):
        nodes = [_node(str(i)) for i in range(5)]
        plan = build_plan(nodes, [])
        assert set(plan.by_id.keys()) == {str(i) for i in range(5)}


# ---------------------------------------------------------------------------
# in_edges_by_node index
# ---------------------------------------------------------------------------

class TestInEdgesIndex:
    def test_source_has_empty_in_edges(self):
        nodes = [_node("src"), _node("dst")]
        edges = [_edge("src", "dst")]
        plan = build_plan(nodes, edges)
        assert plan.in_edges_by_node["src"] == []

    def test_destination_has_one_edge(self):
        nodes = [_node("a"), _node("b")]
        edges = [_edge("a", "b")]
        plan = build_plan(nodes, edges)
        assert len(plan.in_edges_by_node["b"]) == 1
        assert plan.in_edges_by_node["b"][0]["from"] == "a"
        assert plan.in_edges_by_node["b"][0]["to"] == "b"

    def test_fan_in_two_edges(self):
        nodes = [_node("a"), _node("b"), _node("join")]
        edges = [_edge("a", "join"), _edge("b", "join")]
        plan = build_plan(nodes, edges)
        froms = {e["from"] for e in plan.in_edges_by_node["join"]}
        assert froms == {"a", "b"}

    def test_port_notation_preserved(self):
        """Split-op port edges like 'router.pass' are stored verbatim."""
        nodes = [_node("router"), _node("yes")]
        edges = [{"from": "router.pass", "to": "yes"}]
        plan = build_plan(nodes, edges)
        assert plan.in_edges_by_node["yes"][0]["from"] == "router.pass"

    def test_isolated_node_has_empty_list(self):
        nodes = [_node("solo")]
        plan = build_plan(nodes, [])
        assert plan.in_edges_by_node["solo"] == []


# ---------------------------------------------------------------------------
# consumer_counts
# ---------------------------------------------------------------------------

class TestConsumerCounts:
    def test_sink_has_zero_consumers(self):
        nodes = [_node("a"), _node("b")]
        edges = [_edge("a", "b")]
        plan = build_plan(nodes, edges)
        assert plan.consumer_counts["b"] == 0

    def test_single_downstream(self):
        nodes = [_node("a"), _node("b")]
        edges = [_edge("a", "b")]
        plan = build_plan(nodes, edges)
        assert plan.consumer_counts["a"] == 1

    def test_fan_out_two_consumers(self):
        nodes = [_node("src"), _node("t1"), _node("t2")]
        edges = [_edge("src", "t1"), _edge("src", "t2")]
        plan = build_plan(nodes, edges)
        assert plan.consumer_counts["src"] == 2

    def test_isolated_node_zero_consumers(self):
        plan = build_plan([_node("solo")], [])
        assert plan.consumer_counts["solo"] == 0

    def test_middle_node_one_consumer(self):
        nodes = [_node("a"), _node("b"), _node("c")]
        edges = [_edge("a", "b"), _edge("b", "c")]
        plan = build_plan(nodes, edges)
        assert plan.consumer_counts["a"] == 1
        assert plan.consumer_counts["b"] == 1
        assert plan.consumer_counts["c"] == 0

    def test_counts_are_independent_across_calls(self):
        """Mutating one plan's consumer_counts must not affect a fresh build."""
        nodes = [_node("a"), _node("b")]
        edges = [_edge("a", "b")]
        plan1 = build_plan(nodes, edges)
        plan1.consumer_counts["a"] = 99
        plan2 = build_plan(nodes, edges)
        assert plan2.consumer_counts["a"] == 1


# ---------------------------------------------------------------------------
# Full plan validation on a real workflow shape
# ---------------------------------------------------------------------------

class TestLinearPlanComplete:
    """Smoke check: all four plan fields correct on a 3-node linear graph."""

    def setup_method(self):
        self.nodes = [
            _node("src", "source.file"),
            _node("mid", "filter"),
            _node("dst", "target.file"),
        ]
        self.edges = [_edge("src", "mid"), _edge("mid", "dst")]
        self.plan = build_plan(self.nodes, self.edges)

    def test_ordered_ids(self):
        assert self.plan.ordered_ids == ["src", "mid", "dst"]

    def test_by_id_identity(self):
        assert self.plan.by_id["src"] is self.nodes[0]
        assert self.plan.by_id["mid"] is self.nodes[1]
        assert self.plan.by_id["dst"] is self.nodes[2]

    def test_in_edges(self):
        assert self.plan.in_edges_by_node["src"] == []
        assert self.plan.in_edges_by_node["mid"][0]["from"] == "src"
        assert self.plan.in_edges_by_node["dst"][0]["from"] == "mid"

    def test_consumer_counts(self):
        assert self.plan.consumer_counts["src"] == 1
        assert self.plan.consumer_counts["mid"] == 1
        assert self.plan.consumer_counts["dst"] == 0

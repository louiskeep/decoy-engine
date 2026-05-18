"""Unit tests for graph.planner (Sprint 1.1)."""
import pytest

from decoy_engine.graph.planner import (
    ExecutionPlan,
    _count_consumers,
    _resolve_engine_mode,
    ancestor_node_ids,
    build_plan,
    build_preview_plan,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _node(nid, kind="filter"):
    return {"id": nid, "kind": kind}


def _edge(src, dst):
    return {"from": src, "to": dst}


def _config(*node_ids, edges=None, engine=None):
    cfg = {"mode": "graph", "nodes": [_node(nid) for nid in node_ids], "edges": edges or []}
    if engine is not None:
        cfg["engine"] = engine
    return cfg


# ── ExecutionPlan shape ───────────────────────────────────────────────────────

def test_build_plan_linear_graph():
    config = _config("a", "b", "c", edges=[_edge("a", "b"), _edge("b", "c")])
    plan = build_plan(config)
    assert plan.order == ["a", "b", "c"]
    assert set(plan.by_id) == {"a", "b", "c"}
    assert plan.in_edges["a"] == []
    assert len(plan.in_edges["b"]) == 1
    assert plan.in_edges["b"][0]["from"] == "a"
    assert plan.engine_mode == "hybrid"


def test_build_plan_returns_execution_plan_instance():
    plan = build_plan(_config("x"))
    assert isinstance(plan, ExecutionPlan)


def test_build_plan_single_node_no_edges():
    plan = build_plan(_config("solo"))
    assert plan.order == ["solo"]
    assert plan.in_edges["solo"] == []
    assert plan.consumer_counts["solo"] == 0


def test_build_plan_branch_graph_root_first():
    config = _config("a", "b", "c", edges=[_edge("a", "b"), _edge("a", "c")])
    plan = build_plan(config)
    assert plan.order[0] == "a"
    assert set(plan.order[1:]) == {"b", "c"}


def test_build_plan_consumer_counts_linear():
    config = _config("a", "b", "c", edges=[_edge("a", "b"), _edge("b", "c")])
    plan = build_plan(config)
    assert plan.consumer_counts["a"] == 1
    assert plan.consumer_counts["b"] == 1
    assert plan.consumer_counts["c"] == 0


def test_build_plan_consumer_counts_fan_out():
    config = _config("a", "b", "c", edges=[_edge("a", "b"), _edge("a", "c")])
    plan = build_plan(config)
    assert plan.consumer_counts["a"] == 2


def test_build_plan_keep_nodes_bumps_count():
    config = _config("a", "b", edges=[_edge("a", "b")])
    plan = build_plan(config, keep_nodes=["b"])
    assert plan.consumer_counts["b"] == 1


def test_build_plan_keep_nodes_already_has_consumers():
    config = _config("a", "b", "c", edges=[_edge("a", "b"), _edge("b", "c")])
    plan = build_plan(config, keep_nodes=["b"])
    assert plan.consumer_counts["b"] == 2


def test_build_plan_engine_mode_pandas():
    plan = build_plan(_config("a", engine="pandas"))
    assert plan.engine_mode == "pandas"


def test_build_plan_engine_mode_unknown_defaults_to_hybrid():
    plan = build_plan(_config("a", engine="polars"))
    assert plan.engine_mode == "hybrid"


def test_build_plan_engine_mode_none_defaults_to_hybrid():
    plan = build_plan(_config("a"))
    assert plan.engine_mode == "hybrid"


def test_build_plan_in_edges_indexed_by_dest():
    config = _config("a", "b", "c", edges=[_edge("a", "c"), _edge("b", "c")])
    plan = build_plan(config)
    dests = {e["from"] for e in plan.in_edges["c"]}
    assert dests == {"a", "b"}


# ── ancestor_node_ids ─────────────────────────────────────────────────────────

def test_ancestor_node_ids_linear():
    nodes = [_node("a"), _node("b"), _node("c")]
    edges = [_edge("a", "b"), _edge("b", "c")]
    assert ancestor_node_ids(nodes, edges, "c") == {"a", "b", "c"}


def test_ancestor_node_ids_two_parents():
    nodes = [_node("a"), _node("b"), _node("c")]
    edges = [_edge("a", "c"), _edge("b", "c")]
    assert ancestor_node_ids(nodes, edges, "c") == {"a", "b", "c"}


def test_ancestor_node_ids_excludes_downstream():
    nodes = [_node("a"), _node("b"), _node("c")]
    edges = [_edge("a", "b"), _edge("b", "c")]
    needed = ancestor_node_ids(nodes, edges, "b")
    assert needed == {"a", "b"}
    assert "c" not in needed


def test_ancestor_node_ids_tolerates_ghost_edge_src():
    nodes = [_node("a"), _node("b")]
    edges = [_edge("ghost", "b"), _edge("a", "b")]
    needed = ancestor_node_ids(nodes, edges, "b")
    assert "b" in needed
    assert "a" in needed
    assert "ghost" not in needed


def test_ancestor_node_ids_port_notation():
    nodes = [_node("src"), _node("if1", kind="if"), _node("tgt")]
    edges = [_edge("src", "if1"), _edge("if1.pass", "tgt")]
    assert ancestor_node_ids(nodes, edges, "tgt") == {"src", "if1", "tgt"}


def test_ancestor_node_ids_isolated_node():
    nodes = [_node("a"), _node("b")]
    assert ancestor_node_ids(nodes, [], "a") == {"a"}


# ── build_preview_plan ────────────────────────────────────────────────────────

def test_build_preview_plan_scopes_to_ancestors():
    config = {
        "mode": "graph",
        "nodes": [_node("a"), _node("b"), _node("c"), _node("d")],
        "edges": [_edge("a", "b"), _edge("b", "c"), _edge("c", "d")],
    }
    plan = build_preview_plan(config, "c")
    assert set(plan.order) == {"a", "b", "c"}
    assert "d" not in plan.order


def test_build_preview_plan_order_is_topological():
    config = {
        "mode": "graph",
        "nodes": [_node("a"), _node("b"), _node("c")],
        "edges": [_edge("a", "b"), _edge("b", "c")],
    }
    plan = build_preview_plan(config, "c")
    assert plan.order == ["a", "b", "c"]


def test_build_preview_plan_single_node():
    config = {"mode": "graph", "nodes": [_node("solo")], "edges": []}
    plan = build_preview_plan(config, "solo")
    assert plan.order == ["solo"]


# ── _count_consumers ──────────────────────────────────────────────────────────

def test_count_consumers_no_edges():
    nodes = [_node("a"), _node("b")]
    assert _count_consumers(nodes, []) == {"a": 0, "b": 0}


def test_count_consumers_self_edge_ignored():
    nodes = [_node("a")]
    assert _count_consumers(nodes, [{"from": "a", "to": "a"}]) == {"a": 0}


def test_count_consumers_linear():
    nodes = [_node("a"), _node("b")]
    assert _count_consumers(nodes, [_edge("a", "b")]) == {"a": 1, "b": 0}


# ── _resolve_engine_mode ──────────────────────────────────────────────────────

def test_resolve_engine_mode_defaults_hybrid():
    assert _resolve_engine_mode({}) == "hybrid"


def test_resolve_engine_mode_pandas():
    assert _resolve_engine_mode({"engine": "pandas"}) == "pandas"


def test_resolve_engine_mode_bad_value_returns_hybrid():
    assert _resolve_engine_mode({"engine": "duckdb"}) == "hybrid"


def test_resolve_engine_mode_none_returns_hybrid():
    assert _resolve_engine_mode({"engine": None}) == "hybrid"

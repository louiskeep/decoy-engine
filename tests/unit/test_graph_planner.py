"""Unit tests for graph.planner.build_plan.

These tests verify plan shape (topo order, in_edges, consumer_counts,
engine_mode) for a variety of graph configurations. No op execution
happens here; build_plan is pure graph-structure computation.

Sprint 1.1 - Planner Extraction.
"""

from __future__ import annotations

import pytest

from decoy_engine.graph.planner import ExecutionPlan, build_plan


def _cfg(nodes, edges=None, engine=None):
    """Build a minimal graph config dict for planner tests."""
    cfg = {"mode": "graph", "nodes": nodes, "edges": edges or []}
    if engine is not None:
        cfg["engine"] = engine
    return cfg


# ---------------------------------------------------------------------------
# Linear graphs
# ---------------------------------------------------------------------------


def test_linear_order():
    """source -> mask -> target produces that exact topo order."""
    config = _cfg(
        nodes=[
            {"id": "src", "kind": "source.file", "config": {"path": "x.csv"}},
            {"id": "msk", "kind": "mask", "config": {}},
            {"id": "tgt", "kind": "target.file", "config": {"output_filename": "o.csv"}},
        ],
        edges=[
            {"from": "src", "to": "msk"},
            {"from": "msk", "to": "tgt"},
        ],
    )
    plan = build_plan(config)
    assert list(plan.order) == ["src", "msk", "tgt"]


def test_linear_in_edges():
    config = _cfg(
        nodes=[
            {"id": "src", "kind": "source.file", "config": {}},
            {"id": "tgt", "kind": "target.file", "config": {"output_filename": "o.csv"}},
        ],
        edges=[{"from": "src", "to": "tgt"}],
    )
    plan = build_plan(config)
    assert plan.in_edges["src"] == []
    assert plan.in_edges["tgt"] == ["src"]


def test_linear_consumer_counts():
    """source->mask->target: source has 1 consumer, mask has 1, target has 0."""
    config = _cfg(
        nodes=[
            {"id": "src", "kind": "source.file", "config": {}},
            {"id": "msk", "kind": "mask", "config": {}},
            {"id": "tgt", "kind": "target.file", "config": {"output_filename": "o.csv"}},
        ],
        edges=[
            {"from": "src", "to": "msk"},
            {"from": "msk", "to": "tgt"},
        ],
    )
    plan = build_plan(config)
    assert plan.consumer_counts["src"] == 1
    assert plan.consumer_counts["msk"] == 1
    assert plan.consumer_counts["tgt"] == 0


# ---------------------------------------------------------------------------
# Branch fan-out
# ---------------------------------------------------------------------------


def test_branch_consumer_counts():
    """source feeding two masks: source has consumer_count=2."""
    config = _cfg(
        nodes=[
            {"id": "src", "kind": "source.file", "config": {}},
            {"id": "ma", "kind": "mask", "config": {}},
            {"id": "mb", "kind": "mask", "config": {}},
        ],
        edges=[
            {"from": "src", "to": "ma"},
            {"from": "src", "to": "mb"},
        ],
    )
    plan = build_plan(config)
    assert plan.consumer_counts["src"] == 2
    assert plan.consumer_counts["ma"] == 0
    assert plan.consumer_counts["mb"] == 0


def test_branch_in_edges():
    config = _cfg(
        nodes=[
            {"id": "src", "kind": "source.file", "config": {}},
            {"id": "ma", "kind": "mask", "config": {}},
            {"id": "mb", "kind": "mask", "config": {}},
        ],
        edges=[
            {"from": "src", "to": "ma"},
            {"from": "src", "to": "mb"},
        ],
    )
    plan = build_plan(config)
    assert plan.in_edges["ma"] == ["src"]
    assert plan.in_edges["mb"] == ["src"]


# ---------------------------------------------------------------------------
# Split op (if router)
# ---------------------------------------------------------------------------


def test_split_op_produces_port_keys():
    """An 'if' router should produce 'router.pass' and 'router.fail' cache keys."""
    config = _cfg(
        nodes=[
            {"id": "src", "kind": "source.file", "config": {}},
            {"id": "router", "kind": "if", "config": {"predicate": "age >= 18"}},
            {"id": "tgt_pass", "kind": "target.file", "config": {"output_filename": "a.csv"}},
            {"id": "tgt_fail", "kind": "target.file", "config": {"output_filename": "b.csv"}},
        ],
        edges=[
            {"from": "src", "to": "router"},
            {"from": "router.pass", "to": "tgt_pass"},
            {"from": "router.fail", "to": "tgt_fail"},
        ],
    )
    plan = build_plan(config)
    # Split op: per-port keys, not a bare node key.
    assert "router.pass" in plan.consumer_counts
    assert "router.fail" in plan.consumer_counts
    assert "router" not in plan.consumer_counts
    assert plan.consumer_counts["router.pass"] == 1
    assert plan.consumer_counts["router.fail"] == 1


def test_split_op_in_edges_use_port_notation():
    config = _cfg(
        nodes=[
            {"id": "src", "kind": "source.file", "config": {}},
            {"id": "router", "kind": "if", "config": {"predicate": "x > 0"}},
            {"id": "tgt", "kind": "target.file", "config": {"output_filename": "a.csv"}},
        ],
        edges=[
            {"from": "src", "to": "router"},
            {"from": "router.pass", "to": "tgt"},
        ],
    )
    plan = build_plan(config)
    # in_edges preserves the full port notation.
    assert plan.in_edges["tgt"] == ["router.pass"]


# ---------------------------------------------------------------------------
# Engine mode
# ---------------------------------------------------------------------------


def test_default_engine_mode_is_hybrid():
    config = _cfg(nodes=[{"id": "n", "kind": "source.file", "config": {}}])
    plan = build_plan(config)
    assert plan.graph_engine_mode == "hybrid"


def test_explicit_pandas_mode():
    config = _cfg(
        nodes=[{"id": "n", "kind": "source.file", "config": {}}],
        engine="pandas",
    )
    plan = build_plan(config)
    assert plan.graph_engine_mode == "pandas"


def test_unknown_engine_mode_falls_back_to_hybrid():
    config = _cfg(
        nodes=[{"id": "n", "kind": "source.file", "config": {}}],
        engine="polars_only",
    )
    plan = build_plan(config)
    assert plan.graph_engine_mode == "hybrid"


# ---------------------------------------------------------------------------
# Isolated node (no edges)
# ---------------------------------------------------------------------------


def test_isolated_source_has_zero_consumers():
    config = _cfg(nodes=[{"id": "src", "kind": "source.file", "config": {}}])
    plan = build_plan(config)
    assert plan.consumer_counts.get("src", 0) == 0
    assert plan.in_edges["src"] == []


def test_single_node_order():
    config = _cfg(nodes=[{"id": "src", "kind": "source.file", "config": {}}])
    plan = build_plan(config)
    assert list(plan.order) == ["src"]


# ---------------------------------------------------------------------------
# Plan type and immutability
# ---------------------------------------------------------------------------


def test_build_plan_returns_execution_plan():
    config = _cfg(nodes=[{"id": "n", "kind": "source.file", "config": {}}])
    plan = build_plan(config)
    assert isinstance(plan, ExecutionPlan)


def test_execution_plan_is_frozen():
    """ExecutionPlan is a frozen dataclass; mutation must raise."""
    config = _cfg(nodes=[{"id": "n", "kind": "source.file", "config": {}}])
    plan = build_plan(config)
    with pytest.raises((AttributeError, TypeError)):
        plan.order = ("new",)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Multi-edge fan-in
# ---------------------------------------------------------------------------


def test_join_fan_in_in_edges():
    """Two sources feeding a join node: join.in_edges lists both."""
    config = _cfg(
        nodes=[
            {"id": "s1", "kind": "source.file", "config": {}},
            {"id": "s2", "kind": "source.file", "config": {}},
            {"id": "u", "kind": "join", "config": {}},
        ],
        edges=[
            {"from": "s1", "to": "u"},
            {"from": "s2", "to": "u"},
        ],
    )
    plan = build_plan(config)
    assert set(plan.in_edges["u"]) == {"s1", "s2"}
    assert plan.consumer_counts["s1"] == 1
    assert plan.consumer_counts["s2"] == 1


# ---------------------------------------------------------------------------
# Purity: same input must produce equal output across calls.
# ---------------------------------------------------------------------------


class TestPlannerPurity:
    """build_plan is documented as a pure function. Verify by calling
    it twice on the same config and asserting the two plans are equal
    field-by-field. A future refactor that introduces hidden mutable
    state would surface here.

    Pattern: property test cited from V2.0-A.3 plan acceptance:
    "Planner is pure: a property test that runs build_plan(cfg, v)
    == build_plan(cfg, v) for 10 fixtures (no nondeterminism)."
    """

    def _fixtures(self) -> list[dict]:
        return [
            _cfg(
                nodes=[
                    {"id": "s", "kind": "source.file", "config": {}},
                ]
            ),
            _cfg(
                nodes=[
                    {"id": "s", "kind": "source.file", "config": {}},
                    {"id": "m", "kind": "mask", "config": {}},
                    {"id": "t", "kind": "target.file", "config": {}},
                ],
                edges=[{"from": "s", "to": "m"}, {"from": "m", "to": "t"}],
            ),
            _cfg(
                nodes=[
                    {"id": "s", "kind": "source.file", "config": {}},
                    {"id": "m1", "kind": "mask", "config": {}},
                    {"id": "m2", "kind": "mask", "config": {}},
                ],
                edges=[{"from": "s", "to": "m1"}, {"from": "s", "to": "m2"}],
            ),
            _cfg(
                nodes=[
                    {"id": "s1", "kind": "source.file", "config": {}},
                    {"id": "s2", "kind": "source.file", "config": {}},
                    {"id": "j", "kind": "join", "config": {}},
                ],
                edges=[{"from": "s1", "to": "j"}, {"from": "s2", "to": "j"}],
            ),
            _cfg(
                nodes=[
                    {"id": "s", "kind": "source.file", "config": {}},
                    {"id": "m", "kind": "mask", "config": {}},
                ],
                edges=[{"from": "s", "to": "m"}],
                engine="pandas",
            ),
        ]

    def test_repeat_calls_produce_equal_plans(self) -> None:
        """Direct property: build_plan(cfg) == build_plan(cfg)."""
        for cfg in self._fixtures():
            plan_a = build_plan(cfg)
            plan_b = build_plan(cfg)
            assert plan_a == plan_b, f"build_plan was nondeterministic for config={cfg!r}"

    def test_build_plan_does_not_mutate_config(self) -> None:
        """Planner must not write back into the caller's config dict
        (Engineering Best Practices section 2.1: validation never
        mutates input; the planner inherits the same contract because
        it reads the same config).
        """
        import copy

        for cfg in self._fixtures():
            snapshot = copy.deepcopy(cfg)
            _ = build_plan(cfg)
            assert cfg == snapshot, f"build_plan mutated input for config={cfg!r}"


# ---------------------------------------------------------------------------
# Ancestor subgraph traversal (used by preview_graph)
# ---------------------------------------------------------------------------


class TestAncestorNodeIds:
    """ancestor_node_ids walks backward from a target and returns every
    node id that ultimately feeds it. Used by preview_graph to prune
    a full graph down to the smallest subgraph needed to render a
    single node's output.
    """

    def test_linear_chain(self) -> None:
        """source -> mask -> target. Ancestors of target = {source, mask}."""
        from decoy_engine.graph.planner import ancestor_node_ids

        nodes = [
            {"id": "s", "kind": "source.file"},
            {"id": "m", "kind": "mask"},
            {"id": "t", "kind": "target.file"},
        ]
        edges = [{"from": "s", "to": "m"}, {"from": "m", "to": "t"}]
        assert ancestor_node_ids(nodes, edges, "t") == {"s", "m", "t"}
        assert ancestor_node_ids(nodes, edges, "m") == {"s", "m"}
        assert ancestor_node_ids(nodes, edges, "s") == {"s"}

    def test_unreachable_target_returns_only_self(self) -> None:
        from decoy_engine.graph.planner import ancestor_node_ids

        nodes = [
            {"id": "a", "kind": "source.file"},
            {"id": "b", "kind": "source.file"},
        ]
        # No edge between them; b has no ancestors.
        edges: list[dict] = []
        assert ancestor_node_ids(nodes, edges, "b") == {"b"}

    def test_target_not_in_graph_returns_empty(self) -> None:
        from decoy_engine.graph.planner import ancestor_node_ids

        nodes = [{"id": "s", "kind": "source.file"}]
        # Target not present; walk produces nothing.
        assert ancestor_node_ids(nodes, [], "nonexistent") == set()

    def test_tolerates_malformed_entries(self) -> None:
        """The function is "safe" by design: malformed nodes/edges
        are skipped rather than crashed on. Caller passes raw config
        before structural validation."""
        from decoy_engine.graph.planner import ancestor_node_ids

        nodes = [
            {"id": "s", "kind": "source.file"},
            "not-a-dict",
            {"id": 42, "kind": "weird"},  # non-string id
        ]
        edges = [
            {"from": "s", "to": "x"},
            None,  # type: ignore[list-item]
            {"from": 42, "to": "s"},  # non-string from
        ]
        # Should not raise; should not include the malformed entries.
        result = ancestor_node_ids(nodes, edges, "s")
        assert "s" in result

    def test_split_port_normalizes_to_base_node_id(self) -> None:
        """When an edge's `from` is a split-port key like 'router.pass',
        the walk should treat it as a reference to the base node 'router'."""
        from decoy_engine.graph.planner import ancestor_node_ids

        nodes = [
            {"id": "src", "kind": "source.file"},
            {"id": "router", "kind": "if"},
            {"id": "downstream", "kind": "mask"},
        ]
        edges = [
            {"from": "src", "to": "router"},
            {"from": "router.pass", "to": "downstream"},
        ]
        assert ancestor_node_ids(nodes, edges, "downstream") == {
            "src",
            "router",
            "downstream",
        }

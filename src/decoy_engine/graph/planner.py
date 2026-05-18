"""graph.planner: pre-execution plan builder for the graph runner.

Separates the "what will run and in what order?" step from the "execute
each node" step. The runner calls build_plan() once before the execution
loop; the returned ExecutionPlan is read-only and safe to inspect from
tests without running any op.

Audit Sprint 1.1 - Planner Extraction.

Primary consumers:
  - graph.runner._execute_graph: uses plan.order, plan.in_edges,
    plan.consumer_counts, plan.graph_engine_mode.
  - tests/unit/test_graph_planner.py: verifies plan shape for many
    graph shapes without needing a full graph run.

Non-goals for Sprint 1.1:
  - preview_graph still builds its own sub-plan inline (Sprint 1.4).
  - No new validation logic; the caller must validate before calling
    build_plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionPlan:
    """Immutable pre-execution plan for a validated graph config.

    Attributes:
        order: Node ids in topological execution order.
        in_edges: Mapping from each node id to a list of incoming edge
            "from" keys. A key is either a plain node id ("src") or a
            split-port key ("router.pass"). Indexed once so the run loop
            never re-scans the full edge list per node.
        consumer_counts: Initial downstream consumer count per cache key
            (node id or split-port key). The runner decrements counts as
            each consumer reads from the cache and evicts at zero.
        graph_engine_mode: Resolved engine mode ("hybrid" or "pandas").
    """

    order: tuple[str, ...]
    in_edges: dict[str, list[str]]
    consumer_counts: dict[str, int]
    graph_engine_mode: str


def build_plan(config: dict[str, Any]) -> ExecutionPlan:
    """Build an ExecutionPlan from a post-validation graph config.

    The caller must have run GraphConfigValidator before calling build_plan.
    This function does not re-validate; it only computes derived graph
    structure needed by the execution loop.
    """
    from decoy_engine.graph.ops import OPS
    from decoy_engine.graph.topo import topo_order

    nodes: list[dict] = config["nodes"]
    edges: list[dict] = config.get("edges") or []
    graph_engine_mode = _resolve_engine_mode(config)

    order = tuple(topo_order(nodes, edges))

    in_edges: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        in_edges.setdefault(e["to"], []).append(e["from"])

    consumer_counts = _count_consumers(nodes, edges, OPS)

    return ExecutionPlan(
        order=order,
        in_edges=in_edges,
        consumer_counts=consumer_counts,
        graph_engine_mode=graph_engine_mode,
    )


def _resolve_engine_mode(config: dict[str, Any]) -> str:
    mode = config.get("engine") or "hybrid"
    if mode not in ("pandas", "hybrid"):
        return "hybrid"
    return mode


def _count_consumers(
    nodes: list[dict],
    edges: list[dict],
    ops: dict,
) -> dict[str, int]:
    """Per-node (or per split-port) downstream consumer count.

    Split ops (OUTPUT_KIND='split') get per-port entries keyed as
    'node_id.port'. All other ops get a single 'node_id' entry.
    The runner decrements these counts as consumers read from the cache
    and evicts cache entries that reach zero.
    """
    counts: dict[str, int] = {}
    for n in nodes:
        op = ops.get(n.get("kind", "")) if hasattr(ops, "get") else None
        if op is not None and getattr(op, "OUTPUT_KIND", "stream") == "split":
            for port in getattr(op, "OUTPUT_PORTS", ()):
                counts[f"{n['id']}.{port}"] = 0
        else:
            counts[n["id"]] = 0

    for e in edges:
        src = e["from"]
        if src != e["to"] and src in counts:
            counts[src] += 1
    return counts

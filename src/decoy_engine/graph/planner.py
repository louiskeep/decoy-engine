"""graph.planner: pre-execution plan builder for the graph runner.

Separates the "what will run and in what order?" step from the "execute
each node" step. The runner calls build_plan() once before the execution
loop; the returned ExecutionPlan is read-only and safe to inspect from
tests without running any op.

Primary consumers:
  - graph.runner._execute_graph: uses plan.order, plan.in_edges,
    plan.consumer_counts, plan.graph_engine_mode.
  - tests/unit/test_graph_planner.py: verifies plan shape for many
    graph shapes without needing a full graph run.

Design notes:
  - preview_graph builds its own sub-plan on a pruned subgraph rather
    than calling build_plan on the full graph; see runner.preview_graph
    and graph/preview.py.
  - build_plan does not re-validate; callers must run GraphConfigValidator
    first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from decoy_engine.graph.registry import GraphEngineMode


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
    graph_engine_mode: GraphEngineMode


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


def _resolve_engine_mode(config: dict[str, Any]) -> GraphEngineMode:
    mode = config.get("engine") or "hybrid"
    if mode == "pandas":
        return "pandas"
    return "hybrid"


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


def ancestor_node_ids(nodes: list, edges: list, target: str) -> set[str]:
    """Walk backward from ``target`` along edges and return every node id
    that ultimately feeds it.

    Used by ``preview_graph`` to prune a full graph down to the minimum
    subgraph needed to render the requested node. Tolerates malformed
    entries (anything that is not a dict or has a non-string id/from/to
    is skipped rather than crashed on) because the caller passes raw
    config; structural validation has not necessarily run yet.

    Moved here from runner.py (V2.0-A.3, 2026-05-23) because the
    function is structural-graph planning, not execution. Lives next to
    build_plan() for the same reason: anything that walks the graph's
    structure before execution belongs in the planner module.
    """
    valid_ids = {n.get("id") for n in nodes if isinstance(n, dict) and isinstance(n.get("id"), str)}
    in_edges: dict[str, list[str]] = {}
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        src = e.get("from")
        dst = e.get("to")
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        in_edges.setdefault(dst, []).append(src.split(".", 1)[0])

    needed: set[str] = set()
    stack = [target]
    while stack:
        nid = stack.pop()
        if nid in needed or nid not in valid_ids:
            continue
        needed.add(nid)
        stack.extend(in_edges.get(nid, []))
    return needed

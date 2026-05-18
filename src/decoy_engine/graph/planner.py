"""Graph execution planner.

Parses a validated graph config into an ExecutionPlan: ordered node list,
edge index, per-node consumer counts, and resolved engine mode. The runner
asks the planner once per run and executes the plan without re-scanning
graph structure inside the hot loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from decoy_engine.graph.topo import topo_order, upstream_subgraph


@dataclass
class ExecutionPlan:
    """Pre-computed graph structure for a single run or preview."""

    order: list[str]
    by_id: dict[str, dict]
    nodes: list[dict]
    edges: list[dict]
    in_edges: dict[str, list[dict]]
    consumer_counts: dict[str, int]
    engine_mode: str


def build_plan(
    config: dict,
    keep_nodes: list[str] | None = None,
) -> ExecutionPlan:
    """Build an ExecutionPlan from a validated graph config.

    `keep_nodes` bumps the consumer count for named nodes so the runner
    can return their outputs after execution (used by sub_pipeline and
    iterator ops).
    """
    edges = config.get("edges") or []
    nodes = config["nodes"]
    order = topo_order(nodes, edges)
    by_id = {n["id"]: n for n in nodes}
    engine_mode = _resolve_engine_mode(config)

    in_edges: dict[str, list[dict]] = {nid: [] for nid in order}
    for e in edges:
        dst = e.get("to")
        if dst in in_edges:
            in_edges[dst].append(e)

    consumer_counts = _count_consumers(nodes, edges)
    if keep_nodes:
        for k in keep_nodes:
            consumer_counts[k] = consumer_counts.get(k, 0) + 1

    return ExecutionPlan(
        order=order,
        by_id=by_id,
        nodes=nodes,
        edges=edges,
        in_edges=in_edges,
        consumer_counts=consumer_counts,
        engine_mode=engine_mode,
    )


def build_preview_plan(config: dict, node_id: str) -> ExecutionPlan:
    """Build an ExecutionPlan scoped to `node_id`'s ancestor subgraph.

    Assumes the config has already been validated and filtered to the
    ancestor subgraph by the caller (preview_graph).
    """
    nodes = config["nodes"]
    edges = config.get("edges") or []
    sub_order, sub_edges = upstream_subgraph(nodes, edges, node_id)
    sub_node_set = set(sub_order)
    sub_nodes = [n for n in nodes if n["id"] in sub_node_set]

    consumer_counts = _count_consumers(sub_nodes, sub_edges)
    by_id = {n["id"]: n for n in nodes}
    in_edges: dict[str, list[dict]] = {nid: [] for nid in sub_order}
    for e in sub_edges:
        dst = e.get("to")
        if dst in in_edges:
            in_edges[dst].append(e)

    return ExecutionPlan(
        order=sub_order,
        by_id=by_id,
        nodes=sub_nodes,
        edges=sub_edges,
        in_edges=in_edges,
        consumer_counts=consumer_counts,
        engine_mode=_resolve_engine_mode(config),
    )


def ancestor_node_ids(nodes: list, edges: list, target: str) -> set[str]:
    """Walk upward from `target` collecting ancestors, tolerant of
    malformed nodes/edges in unrelated parts of the graph.

    Used by preview_graph to scope validation to the ancestor subgraph
    so broken downstream nodes don\'t block sampling at an upstream node.
    """
    valid_ids = {
        n.get("id") for n in nodes
        if isinstance(n, dict) and isinstance(n.get("id"), str)
    }
    in_map: dict[str, list[str]] = {}
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        src = e.get("from")
        dst = e.get("to")
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        in_map.setdefault(dst, []).append(src.split(".", 1)[0])

    needed: set[str] = set()
    stack = [target]
    while stack:
        nid = stack.pop()
        if nid in needed or nid not in valid_ids:
            continue
        needed.add(nid)
        stack.extend(in_map.get(nid, []))
    return needed


def _resolve_engine_mode(config: dict) -> str:
    mode = config.get("engine") or "hybrid"
    if mode not in ("pandas", "hybrid"):
        return "hybrid"
    return mode


def _count_consumers(nodes: list[dict], edges: list[dict]) -> dict[str, int]:
    """Per node (or per port for split ops), count downstream edge consumers."""
    from decoy_engine.graph.ops import OPS

    counts: dict[str, int] = {}
    for n in nodes:
        op = OPS.get(n.get("kind", "")) if hasattr(OPS, "get") else None
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

"""Graph execution planner: topology, edge indexing, and consumer counts.

This module answers "what do I need to know about a graph before executing
it?" without doing any actual execution. The runner asks build_plan() for a
GraphPlan, then uses it throughout the run loop instead of recomputing graph
shape inline.

Sprint 1.1 (graph runtime split): extracted from runner.py so graph shape
computation can be tested independently and future ops can rely on plan data
without scanning all edges themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decoy_engine.graph.topo import topo_order


@dataclass
class GraphPlan:
    """Pre-computed execution plan for a graph.

    Built once from nodes + edges before any op runs. The consumer_counts
    dict is owned by the plan; callers that need a mutable eviction tracker
    should take a shallow copy (``dict(plan.consumer_counts)``) so the plan
    itself remains inspectable and reusable.

    Fields:
        ordered_ids: Node IDs in a valid topological execution order.
        by_id: Node dict indexed by node ID for O(1) lookup during the run loop.
        in_edges_by_node: Incoming edge dicts indexed by destination node ID.
            Each list contains edge dicts (with ``"from"`` and ``"to"`` keys)
            that arrive at the node. Source nodes have an empty list.
        consumer_counts: Downstream consumer count per node output.
            Split ops (OUTPUT_KIND="split") use per-port keys
            ``"node_id.port"`` rather than a plain ``"node_id"`` key, matching
            the runner's cache key convention so eviction math is correct.
    """

    ordered_ids: list[str]
    by_id: dict[str, dict]
    in_edges_by_node: dict[str, list[dict]]
    consumer_counts: dict[str, int]


def build_plan(nodes: list[dict], edges: list[dict]) -> GraphPlan:
    """Build an execution plan from nodes and edges.

    Validates graph topology (raises ValidationError on cycles) and
    pre-computes the indexing structures the runner needs:

    - Topological execution order via Kahn's algorithm (from topo.py).
    - Node-ID index for O(1) node lookup.
    - Per-node incoming edge index so the runner doesn't scan all edges
      per node in the run loop (O(E) per node → O(1)).
    - Consumer counts for cache eviction, with per-port entries for split ops.

    Args:
        nodes: List of node dicts from the pipeline config.
        edges: List of edge dicts with ``"from"`` and ``"to"`` keys.

    Returns:
        GraphPlan with all fields populated.

    Raises:
        ValidationError: If the graph contains a cycle.
    """
    ordered_ids = topo_order(nodes, edges)
    by_id = {n["id"]: n for n in nodes}

    in_edges_by_node: dict[str, list[dict]] = {n["id"]: [] for n in nodes}
    for e in edges:
        dst = e.get("to")
        if dst in in_edges_by_node:
            in_edges_by_node[dst].append(e)

    consumer_counts = _compute_consumer_counts(nodes, edges)

    return GraphPlan(
        ordered_ids=ordered_ids,
        by_id=by_id,
        in_edges_by_node=in_edges_by_node,
        consumer_counts=consumer_counts,
    )


def _compute_consumer_counts(nodes: list[dict], edges: list[dict]) -> dict[str, int]:
    """Count downstream edge consumers per node output.

    Split ops (OUTPUT_KIND="split") get per-port entries keyed as
    ``"node_id.port"`` rather than a single ``"node_id"`` entry. This mirrors
    the runner's cache key convention so eviction math is correct.

    Unknown or missing kinds are treated as non-split ops so this helper
    stays usable from validators and tests that pass minimal node dicts.
    """
    from decoy_engine.graph.ops import OPS

    counts: dict[str, int] = {}
    for n in nodes:
        kind = n.get("kind", "")
        op = OPS.get(kind)
        if op is not None and getattr(op, "OUTPUT_KIND", "stream") == "split":
            for port in getattr(op, "OUTPUT_PORTS", ()):
                counts[f"{n['id']}.{port}"] = 0
        else:
            counts[n["id"]] = 0

    for e in edges:
        src = e.get("from", "")
        dst = e.get("to", "")
        if src != dst and src in counts:
            counts[src] += 1
    return counts

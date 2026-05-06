"""Topological sort for graph-mode pipelines.

Kahn's algorithm. Raises ValidationError on cycles. Edges are dicts with
`from`/`to` keys (the YAML format), not the in-code `from_` form.
"""

from collections import deque
from typing import Iterable

from decoy_engine.internal.validator import ValidationError


def topo_order(nodes: Iterable[dict], edges: Iterable[dict]) -> list[str]:
    """Return node ids in a valid execution order.

    Raises ValidationError("graph has a cycle", "edges") if no order exists.
    """
    node_ids = [n["id"] for n in nodes]
    indegree = {nid: 0 for nid in node_ids}
    out_edges: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for e in edges:
        src, dst = e["from"], e["to"]
        out_edges[src].append(dst)
        indegree[dst] += 1

    ready = deque(nid for nid, d in indegree.items() if d == 0)
    order: list[str] = []
    while ready:
        nid = ready.popleft()
        order.append(nid)
        for dst in out_edges[nid]:
            indegree[dst] -= 1
            if indegree[dst] == 0:
                ready.append(dst)

    if len(order) != len(node_ids):
        raise ValidationError("graph has a cycle", "edges")
    return order


def upstream_subgraph(
    nodes: Iterable[dict], edges: Iterable[dict], target: str
) -> tuple[list[str], list[dict]]:
    """Return (ordered_node_ids, edges) needed to compute `target`.

    Used by preview_graph: walks the DAG up from `target`, collects every
    ancestor + the target itself, and returns them in topo order.
    """
    out_edges: dict[str, list[str]] = {}
    in_edges: dict[str, list[str]] = {}
    for n in nodes:
        out_edges.setdefault(n["id"], [])
        in_edges.setdefault(n["id"], [])
    for e in edges:
        out_edges.setdefault(e["from"], []).append(e["to"])
        in_edges.setdefault(e["to"], []).append(e["from"])

    needed: set[str] = set()
    stack = [target]
    while stack:
        nid = stack.pop()
        if nid in needed:
            continue
        needed.add(nid)
        stack.extend(in_edges.get(nid, []))

    sub_nodes = [n for n in nodes if n["id"] in needed]
    sub_edges = [e for e in edges if e["from"] in needed and e["to"] in needed]
    return topo_order(sub_nodes, sub_edges), sub_edges

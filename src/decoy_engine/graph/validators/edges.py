"""Graph-structure validation (V2.0-B stage 3-5).

Three stages, all raise on first failure:

  - validate_edges: each edge is a mapping with from/to pointing at
    known node ids; split-op port notation ("node.port") resolves to
    a registered output port.
  - validate_cardinality: every node satisfies its op's INPUT_ARITY
    bounds; sink ops have no outgoing edges.
  - validate_acyclic: the graph has a valid topological order
    (raises on cycle via decoy_engine.graph.topo.topo_order).

These run in sequence in validate_graph_full; each is gated by the
previous one passing.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.errors import ValidationError


def validate_edges(edges: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> None:
    from decoy_engine.graph.ops import OPS

    node_ids = {n["id"] for n in nodes}
    node_by_id = {n["id"]: n for n in nodes}
    for j, edge in enumerate(edges):
        path = f"edges[{j}]"
        if not isinstance(edge, dict):
            raise ValidationError("edge must be a mapping", path)
        src = edge.get("from")
        dst = edge.get("to")

        # Handle "node_id.port" notation for split ops.
        if isinstance(src, str) and "." in src:
            base_nid, port = src.split(".", 1)
            if base_nid not in node_ids:
                raise ValidationError(
                    f"'from' references unknown node {base_nid!r}",
                    f"{path}.from",
                )
            op = OPS[node_by_id[base_nid]["kind"]]
            if getattr(op, "OUTPUT_KIND", "stream") != "split":
                raise ValidationError(
                    f"node {base_nid!r} is not a split op; port notation not allowed",
                    f"{path}.from",
                )
            valid_ports = getattr(op, "OUTPUT_PORTS", ())
            if port not in valid_ports:
                raise ValidationError(
                    f"unknown port {port!r} on split node {base_nid!r} (valid: {valid_ports})",
                    f"{path}.from",
                )
        else:
            if src not in node_ids:
                raise ValidationError(
                    f"'from' references unknown node {src!r}",
                    f"{path}.from",
                )

        if dst not in node_ids:
            raise ValidationError(
                f"'to' references unknown node {dst!r}",
                f"{path}.to",
            )


def validate_cardinality(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    kinds: set[str],
) -> None:
    from decoy_engine.graph.ops import OPS

    in_count: dict[str, int] = {n["id"]: 0 for n in nodes}
    out_count: dict[str, int] = {n["id"]: 0 for n in nodes}
    for e in edges:
        in_count[e["to"]] += 1
        base_src = e["from"].split(".", 1)[0]  # strip port suffix for split ops
        out_count[base_src] += 1

    for n in nodes:
        kind = n["kind"]
        op = OPS[kind]
        arity = getattr(op, "INPUT_ARITY", (1, 1))
        output_kind = getattr(op, "OUTPUT_KIND", "stream")
        ic = in_count[n["id"]]
        oc = out_count[n["id"]]

        min_in, max_in = arity
        if ic < min_in:
            raise ValidationError(
                f"node {n['id']!r} ({kind}) needs at least {min_in} incoming edge(s), got {ic}",
                f"nodes.{n['id']}",
            )
        if max_in is not None and ic > max_in:
            hint = (
                " -- combine upstream tables with a 'join' node first"
                if max_in == 1 and kind != "join"
                else ""
            )
            raise ValidationError(
                f"node {n['id']!r} ({kind}) accepts at most {max_in} "
                f"incoming edge(s), got {ic}{hint}",
                f"nodes.{n['id']}",
            )
        if output_kind == "sink" and oc > 0:
            raise ValidationError(
                f"target node {n['id']!r} must have no outgoing edges (got {oc})",
                f"nodes.{n['id']}",
            )


def validate_acyclic(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """Raises ValidationError on cycle via topo_order."""
    from decoy_engine.graph.topo import topo_order

    topo_order(nodes, edges)

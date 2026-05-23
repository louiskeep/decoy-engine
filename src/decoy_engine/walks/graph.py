"""ER graph construction.

Takes a `SchemaSnapshot` (and optionally a list of inferred edges from
`inference.py`) and builds the bookkeeping that the hazard detectors
need: in-degree / out-degree counts and a directed adjacency list.
Used by `hazards.detect_hazards`; also returned to callers that want
to render the graph or query it in their own way.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from decoy_engine.walks.types import Edge, SchemaSnapshot


@dataclass(frozen=True)
class ERGraph:
    tables: tuple[str, ...]
    edges: tuple[Edge, ...]
    incoming_edge_count: dict[str, int]
    outgoing_edge_count: dict[str, int]
    adjacency: dict[str, tuple[str, ...]]  # source_table -> tuple of target_tables


def build_er_graph(
    snapshot: SchemaSnapshot,
    extra_edges: Iterable[Edge] = (),
) -> ERGraph:
    """Build the ER graph from a snapshot.

    `extra_edges` is typically the output of `infer_edges(snapshot)` —
    callers that want only declared edges pass `()`. Edges from both
    sources are merged; downstream code uses `Edge.declared` to tell
    them apart when needed.
    """
    edges_list = list(snapshot.declared_edges) + list(extra_edges)

    tables = tuple(t.name for t in snapshot.tables)
    incoming = {t: 0 for t in tables}
    outgoing = {t: 0 for t in tables}
    adjacency: dict[str, list[str]] = {t: [] for t in tables}

    for edge in edges_list:
        # Defensive: edges may reference tables not in the snapshot's
        # `tables` list (cross-schema edges, or stale snapshots).
        # Initialize on the fly so the counts are consistent.
        if edge.source_table not in incoming:
            incoming[edge.source_table] = 0
            outgoing[edge.source_table] = 0
            adjacency[edge.source_table] = []
        if edge.target_table not in incoming:
            incoming[edge.target_table] = 0
            outgoing[edge.target_table] = 0
            adjacency[edge.target_table] = []

        outgoing[edge.source_table] += 1
        incoming[edge.target_table] += 1
        adjacency[edge.source_table].append(edge.target_table)

    return ERGraph(
        tables=tables,
        edges=tuple(edges_list),
        incoming_edge_count=incoming,
        outgoing_edge_count=outgoing,
        adjacency={k: tuple(v) for k, v in adjacency.items()},
    )

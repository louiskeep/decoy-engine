"""Schema-shape hazard detection.

Six detector functions, one per kind. Each takes the snapshot + ER
graph and returns a tuple of `Hazard` objects. `detect_hazards`
composes them.

The taxonomy comes from the chaser stress-test schema commentary
(`decoy-platform/plans/chaser-stress-test-schema.sql`):

    HUB — table referenced by many others (in-degree above threshold)
    SR  — self-reference (table FKs to itself)
    PE  — parallel edges (multiple FKs from one table to the same target)
    PM  — polymorphic FK (entity_type + entity_id; no enforceable constraint)
    ALT — alternative parents (XOR — one of N nullable FKs is set per row)
    CIR — cycle in the FK graph

Detectors that need data inspection (PM, ALT) are conservative: they
fire only when the schema's *shape* is a strong signal. PM looks for
the `<prefix>_type` + `<prefix>_id` pattern with the `_id` lacking
a declared FK. ALT looks for tables with multiple nullable FKs to
different parents — the intent is "exactly one is set" but we can't
verify without scanning data, so we surface as a hazard for the user
to confirm.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from decoy_engine.walks.graph import ERGraph, build_er_graph
from decoy_engine.walks.types import Column, Edge, Hazard, SchemaSnapshot

# Threshold above which a table's in-degree counts as a HUB. The
# chaser stress-test schema's `users` and `issues` are deliberately
# above 10; small schemas rarely cross 5. 5 is the sweet spot for
# mid-market warehouses; tunable later via config.
_HUB_INCOMING_THRESHOLD = 5


def detect_hazards(
    snapshot: SchemaSnapshot,
    extra_edges: Iterable[Edge] = (),
) -> tuple[Hazard, ...]:
    """Run every detector against the snapshot and return the union.

    `extra_edges` lets callers fold in heuristic edges (typically the
    output of `inference.infer_edges`) before hazard detection. Build
    the graph once and share it across detectors so we don't re-walk
    the schema multiple times.
    """
    graph = build_er_graph(snapshot, extra_edges)
    hazards: list[Hazard] = []
    hazards.extend(_detect_hub(graph))
    hazards.extend(_detect_self_reference(graph))
    hazards.extend(_detect_parallel_edges(graph))
    hazards.extend(_detect_polymorphic_fk(snapshot, graph))
    hazards.extend(_detect_alternative_parents(snapshot, graph))
    hazards.extend(_detect_cycles(graph))

    # Stable ordering by (kind, table) so test assertions and UI
    # rendering don't depend on dict iteration order.
    hazards.sort(key=lambda h: (h.kind, h.table or "", h.description))
    return tuple(hazards)


# ── HUB ───────────────────────────────────────────────────────────────


def _detect_hub(graph: ERGraph) -> list[Hazard]:
    """Tables referenced by lots of others. They become visualization
    hubs in the ER graph and they're typically the high-value PII
    tables (users, customers, patients) — worth flagging to the
    operator who's deciding what to mask."""
    hazards: list[Hazard] = []
    for table, in_degree in graph.incoming_edge_count.items():
        if in_degree >= _HUB_INCOMING_THRESHOLD:
            hazards.append(
                Hazard(
                    kind="HUB",
                    table=table,
                    description=f"{table} is referenced by {in_degree} other tables",
                    details={"incoming_edge_count": in_degree},
                )
            )
    return hazards


# ── SR ────────────────────────────────────────────────────────────────


def _detect_self_reference(graph: ERGraph) -> list[Hazard]:
    """A table FKs to itself: parent/child trees, manager/employee,
    reply-to comments. Worth flagging because the masker has to be
    careful about FK-stable substitution (mask both ends consistently)
    and the chase BFS has to handle the cycle."""
    hazards: list[Hazard] = []
    seen: set[tuple[str, str]] = set()
    for edge in graph.edges:
        if edge.source_table == edge.target_table:
            key = (edge.source_table, edge.source_column)
            if key in seen:
                continue
            seen.add(key)
            hazards.append(
                Hazard(
                    kind="SR",
                    table=edge.source_table,
                    description=(
                        f"{edge.source_table}.{edge.source_column} references "
                        f"{edge.source_table}.{edge.target_column} (self-reference)"
                    ),
                    details={"column": edge.source_column},
                )
            )
    return hazards


# ── PE ────────────────────────────────────────────────────────────────


def _detect_parallel_edges(graph: ERGraph) -> list[Hazard]:
    """Multiple FKs from one table to the same target. Examples from
    the chaser fixture: issues has assignee_id / reporter_id /
    created_by_id / resolved_by_id all pointing at users; status
    transitions has from_status_id + to_status_id both pointing at
    statuses. Hazard surfaces because chase / mask need to know which
    column to traverse."""
    hazards: list[Hazard] = []
    pairs: dict[tuple[str, str], list[Edge]] = defaultdict(list)
    for edge in graph.edges:
        if edge.source_table != edge.target_table:  # SR is its own kind
            pairs[(edge.source_table, edge.target_table)].append(edge)

    for (source, target), edges in pairs.items():
        if len(edges) > 1:
            cols = sorted({e.source_column for e in edges})
            hazards.append(
                Hazard(
                    kind="PE",
                    table=source,
                    description=(
                        f"{source} has {len(edges)} FKs to {target} (columns: {', '.join(cols)})"
                    ),
                    details={
                        "target_table": target,
                        "source_columns": cols,
                    },
                )
            )
    return hazards


# ── PM ────────────────────────────────────────────────────────────────


def _detect_polymorphic_fk(
    snapshot: SchemaSnapshot,
    graph: ERGraph,
) -> list[Hazard]:
    """Polymorphic FK: a table has both `<prefix>_type` and `<prefix>_id`
    columns where the `_id` doesn't have a declared FK. Pattern means
    "id points at one of N tables, type tells you which" — a real DB
    constraint is impossible because the target depends on the row's
    type value.

    Conservative: only fires when both columns exist AND the `_id` has
    no declared FK. False positive (an `entity_type` that isn't a
    discriminator) is acceptable because a human reviews each hazard.
    """
    hazards: list[Hazard] = []
    declared_pairs: set[tuple[str, str]] = {
        (e.source_table, e.source_column) for e in snapshot.declared_edges
    }
    for table in snapshot.tables:
        col_names = {c.name.lower() for c in table.columns}
        for col in table.columns:
            name = col.name.lower()
            if not name.endswith("_type"):
                continue
            prefix = name[:-5]  # strip "_type"
            id_col = f"{prefix}_id"
            if id_col not in col_names:
                continue
            # The id column exists; check it has no declared FK. Some
            # apps DO declare a default FK pointing at "the most common
            # entity type" — not strictly polymorphic, so skip those.
            if (table.name, id_col) in declared_pairs:
                continue
            hazards.append(
                Hazard(
                    kind="PM",
                    table=table.name,
                    description=(
                        f"{table.name}.{id_col} is polymorphic — target depends "
                        f"on {table.name}.{col.name}"
                    ),
                    details={
                        "type_column": col.name,
                        "id_column": id_col,
                    },
                )
            )
    return hazards


# ── ALT ───────────────────────────────────────────────────────────────


def _detect_alternative_parents(
    snapshot: SchemaSnapshot,
    graph: ERGraph,
) -> list[Hazard]:
    """ALT (XOR alternative parents): a table with multiple nullable
    FK columns to different parent tables, each NULL-able, where the
    runtime invariant is "exactly one is set per row."

    Detecting this perfectly needs a CHECK constraint or data scan;
    we approximate by surfacing tables that have 2+ nullable declared
    FKs pointing at *different* parents. The user confirms whether
    the "exactly one" semantic actually holds.
    """
    hazards: list[Hazard] = []
    cols_by_table: dict[str, dict[str, Column]] = {
        t.name: {c.name: c for c in t.columns} for t in snapshot.tables
    }

    by_source: dict[str, list[Edge]] = defaultdict(list)
    for edge in snapshot.declared_edges:
        by_source[edge.source_table].append(edge)

    for source_table, edges in by_source.items():
        # Group by source column so multi-column FKs count once
        nullable_singletons: list[Edge] = []
        cols = cols_by_table.get(source_table, {})
        for edge in edges:
            col = cols.get(edge.source_column)
            if col is not None and col.nullable:
                nullable_singletons.append(edge)
        targets = {e.target_table for e in nullable_singletons}
        if len(targets) >= 2:
            hazards.append(
                Hazard(
                    kind="ALT",
                    table=source_table,
                    description=(
                        f"{source_table} has nullable FKs to "
                        f"{len(targets)} different parents: {', '.join(sorted(targets))}"
                    ),
                    details={
                        "parent_tables": sorted(targets),
                        "source_columns": sorted({e.source_column for e in nullable_singletons}),
                    },
                )
            )
    return hazards


# ── CIR ───────────────────────────────────────────────────────────────


def _detect_cycles(graph: ERGraph) -> list[Hazard]:
    """Find cycles in the FK graph via DFS with white/gray/black
    coloring. Returns one Hazard per distinct cycle found.

    Cycles are normalized — start from the lexicographically smallest
    table in the cycle — so the same cycle reported via two different
    DFS paths only appears once."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {t: WHITE for t in graph.adjacency}

    cycles: set[tuple[str, ...]] = set()

    def visit(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbor in graph.adjacency.get(node, ()):
            if color.get(neighbor, WHITE) == GRAY:
                # Found a back-edge — extract the cycle from path.
                start = path.index(neighbor)
                cycle = tuple(path[start:])
                cycles.add(_canonical_cycle(cycle))
            elif color.get(neighbor, WHITE) == WHITE:
                visit(neighbor, path)
        path.pop()
        color[node] = BLACK

    for node in graph.adjacency:
        if color[node] == WHITE:
            visit(node, [])

    hazards: list[Hazard] = []
    for cycle in sorted(cycles):
        hazards.append(
            Hazard(
                kind="CIR",
                table=None,
                description=f"Cycle: {' -> '.join(cycle)} -> {cycle[0]}",
                details={"cycle": list(cycle)},
            )
        )
    return hazards


def _canonical_cycle(cycle: tuple[str, ...]) -> tuple[str, ...]:
    """Rotate the cycle so it starts at the lexicographically smallest
    table. Same cycle traversed from two different starting points now
    canonicalizes to the same tuple."""
    if not cycle:
        return cycle
    smallest_idx = min(range(len(cycle)), key=lambda i: cycle[i])
    return cycle[smallest_idx:] + cycle[:smallest_idx]

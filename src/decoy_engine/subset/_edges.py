"""Edge normalization: `PlanRelationship` (one-or-many children) -> sorted `SubsetEdge` pairs.

A `PlanRelationship` bundles one parent with a tuple of child ends (P1); the
closure algorithm (`_closure.py`) reasons about (parent, child) PAIRS, so this
module expands and normalizes that shape once, up front, before any of SS2-SS5
run. Dedupe + sort mirror `relationships._graph.build_relationship_graph`'s
identity + ordering rules (audit M1) so subsetting and masking treat a
duplicate relationship declaration identically.

Multi-parent semantics (section 2 of the implementation guide, DECIDED HERE):
each edge is traversed independently. When a child's FK columns reference
several parent tables (WS5 multi-parent FK), an upward pull adds the matching
parent rows in EVERY parent table that contains the key -- unlike masking's
declared-order first-hit-wins lookup. This diverges from masking on purpose:
independent per-edge upward pulls can never dangle (a row is skipped only when
the key is absent from that specific parent, never because a different parent
"won" the lookup), and over-pull is bounded to rows sharing the key (visible in
the estimate).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from decoy_engine import reasons
from decoy_engine.config import classify_table_kinds
from decoy_engine.plan._types import PlanRelationship
from decoy_engine.subset._errors import SubsetPreflightError
from decoy_engine.subset._types import FkPreflightReport, PreflightFailure, SubsetEdge


def _edge_id(
    parent_table: str,
    parent_columns: tuple[str, ...],
    child_table: str,
    child_columns: tuple[str, ...],
) -> str:
    return f"{parent_table}.{','.join(parent_columns)} -> {child_table}.{','.join(child_columns)}"


def build_subset_edges(relationships: tuple[PlanRelationship, ...]) -> tuple[SubsetEdge, ...]:
    """Expand + dedupe + sort `PlanRelationship` tuples into `SubsetEdge` pairs."""
    edges: dict[SubsetEdge, None] = {}
    for rel in relationships:
        for child in rel.children:
            edge = SubsetEdge(
                edge_id=_edge_id(rel.parent.table, rel.parent.columns, child.table, child.columns),
                parent_table=rel.parent.table,
                parent_columns=rel.parent.columns,
                child_table=child.table,
                child_columns=child.columns,
                orphan_policy=rel.orphan_policy,
                namespace=rel.namespace,
            )
            # dict.fromkeys-style dedupe: identity is the full edge tuple (audit M1).
            edges[edge] = None
    return tuple(sorted(edges, key=lambda e: e.edge_id))


def linked_neighbours(relationships: tuple[PlanRelationship, ...]) -> dict[str, set[str]]:
    """Each table named by a relationship mapped to the tables it shares an edge with, either way.

    The one reach walk's graph: `scope_for` and the sample's components both read it, so they
    cannot disagree on what is linked."""
    neighbours: dict[str, set[str]] = {}
    for rel in relationships:
        for child in rel.children:
            neighbours.setdefault(rel.parent.table, set()).add(child.table)
            neighbours.setdefault(child.table, set()).add(rel.parent.table)
    return neighbours


def reach(start: Iterable[str], neighbours: Mapping[str, set[str]]) -> set[str]:
    """Every table linked to `start` over `neighbours`, `start` included. The one walk, so a
    component and a seed's scope cannot be computed two ways."""
    reached = set(start)
    frontier = list(reached)
    while frontier:
        for nxt in neighbours.get(frontier.pop(), ()):
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    return reached


def one_failure_report(failure: PreflightFailure) -> FkPreflightReport:
    """The report of a preflight that stopped on one failure. Callers pass their own registry
    code to the error, so each raise site stays a checked literal."""
    return FkPreflightReport(passed=False, failures=(failure,), warnings=(), edges=())


def unknown_table_error(table: str) -> SubsetPreflightError:
    """The refusal for a table that is not a mask table of the config; names the config key only."""
    message = f"table {table!r} is not a mask table of the config."
    failure = PreflightFailure(
        code=reasons.subset.SUBSET_UNKNOWN_TABLE, relationship=f"<{table}>", message=message
    )
    return SubsetPreflightError(
        code=reasons.subset.SUBSET_UNKNOWN_TABLE,
        report=one_failure_report(failure),
        message=message,
    )


def relationships_from_config(config: dict) -> tuple[PlanRelationship, ...]:
    """Build `PlanRelationship` tuples from a validated `PipelineConfig` dump's `relationships` block.

    GATE-1 #5: this is the SAME declaration surface masking already uses; no new
    surface. `PlanRelationship.__post_init__` enforces composite_columns_length_match;
    a half-declared composite key (mismatched parent/child tuple lengths) raises a
    `ValueError` there, which this adapter catches and re-wraps as a
    `SubsetPreflightError(code="subset_relationship_composite_length")` carrying a
    single-failure `FkPreflightReport` so the caller sees a clean preflight failure
    instead of a stack trace (section 5.2 of the implementation guide).
    """
    from decoy_engine.plan._types import PlanRelationshipEnd

    relationships: list[PlanRelationship] = []
    for entry in config.get("relationships", []):
        parent = entry["parent"]
        children = entry["children"]
        parent_end = PlanRelationshipEnd(table=parent["table"], columns=tuple(parent["columns"]))
        child_ends = tuple(
            PlanRelationshipEnd(table=c["table"], columns=tuple(c["columns"])) for c in children
        )
        try:
            relationships.append(
                PlanRelationship(
                    parent=parent_end,
                    children=child_ends,
                    orphan_policy=entry["orphan_policy"],
                    namespace=entry.get("namespace"),
                )
            )
        except ValueError as exc:
            # One offending child at a time: name the first mismatched pair so the
            # report reads like every other preflight failure (relationship + message).
            for child_end in child_ends:
                if len(child_end.columns) != len(parent_end.columns):
                    edge_id = _edge_id(
                        parent_end.table, parent_end.columns, child_end.table, child_end.columns
                    )
                    break
            else:
                edge_id = f"<{parent_end.table}>"
            failure = PreflightFailure(
                code=reasons.subset.SUBSET_RELATIONSHIP_COMPOSITE_LENGTH,
                relationship=edge_id,
                message=str(exc),
            )
            raise SubsetPreflightError(
                code=reasons.subset.SUBSET_RELATIONSHIP_COMPOSITE_LENGTH,
                report=one_failure_report(failure),
                message=str(exc),
            ) from exc
    return tuple(relationships)


def _generated_tables(config: dict) -> set[str]:
    return {n for n, kind in classify_table_kinds(config).items() if kind == "generate"}


def require_known_endpoints(config: dict) -> None:
    """Refuse the first relationship end that names no table of the config.

    Scan raw entries so every end is checked before relationship structures are built."""
    known = classify_table_kinds(config)
    for entry in config.get("relationships", []):
        parent = entry["parent"]
        for end in (parent, *entry["children"]):
            table = end["table"]
            if table not in known:
                raise unknown_table_error(table)


def mask_relationships(
    config: dict,
) -> tuple[tuple[PlanRelationship, ...], tuple[str, ...]]:
    """The relationships a run can subset, and the sorted ids of the edges dropped.

    A generate table has no source to read, so an edge touching one is dropped, per edge: a mask
    child of a mask parent keeps its edge even when a sibling child is generated, or its rows
    would point at parents the subset removed. The one drop for `subset_request_from_config` and
    `sample_request`.
    """
    generated = _generated_tables(config)
    kept_relationships: list[PlanRelationship] = []
    excluded: list[str] = []
    for rel in relationships_from_config(config):
        kept = (
            ()
            if rel.parent.table in generated
            else tuple(c for c in rel.children if c.table not in generated)
        )
        if len(kept) != len(rel.children):
            dropped = replace(rel, children=tuple(c for c in rel.children if c not in kept))
            excluded.extend(e.edge_id for e in build_subset_edges((dropped,)))
        if kept:
            kept_relationships.append(replace(rel, children=kept))
    # a config may repeat a relationship; the record names each edge once
    return tuple(kept_relationships), tuple(sorted(set(excluded)))


def sample_refusal_edge(config: dict) -> tuple[str, tuple[str, ...], str, tuple[str, ...]] | None:
    """The first edge from a generated parent to a masked child, as `(parent_table,
    parent_columns, child_table, child_columns)`, or `None`.

    Read before `mask_relationships` drops it: a sample scales the generated parent down while
    the masked child keeps its source keys, so the child would point at parents that are gone.
    """
    kinds = classify_table_kinds(config)
    for rel in relationships_from_config(config):
        if kinds.get(rel.parent.table) != "generate":
            continue
        for child in rel.children:
            if kinds.get(child.table) == "mask":
                return rel.parent.table, rel.parent.columns, child.table, child.columns
    return None

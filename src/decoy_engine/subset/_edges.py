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
            report = FkPreflightReport(
                passed=False,
                failures=(
                    PreflightFailure(
                        code="subset_relationship_composite_length",
                        relationship=edge_id,
                        message=str(exc),
                    ),
                ),
                warnings=(),
                edges=(),
            )
            raise SubsetPreflightError(
                code="subset_relationship_composite_length", report=report, message=str(exc)
            ) from exc
    return tuple(relationships)

"""Task 4.6 slice 5b-ii: shadow-side FK dispatch for the ONE admitted
generate-parent -> mask-child edge a mixed dispatch's admission gate
(`_shadow_mixed._select_admitted_coupled_edge`) allowlisted.

The resolution itself (the generate parent's identity map, the shared
`resolve_fk_keys` orphan-policy engine, the lossless write-back that keeps a
key past 2**53 exact, and the REMAP-orphan rejection) lives in
`execution._mixed_fk_bridge`, shared with the production mixed route so the
two cannot drift; `ShadowFkResolution` and `resolve_generate_parent_fk_child`
stay importable from here. What remains in this module is the shadow's
per-node plumbing: the run-scoped admitted-edge allowlist, the classification
of an FK-child node against it, and the coded decline for an FK child that is
not on the list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from decoy_engine.execution._mixed_fk_bridge import FkResolution, resolve_generate_parent_fk_child
from decoy_engine.execution.physical._shadow_diff_codes import (
    DUPLICATE_NODE_DECLARATION,
    MIXED_FK_UNADMITTED_CHILD,
    ShadowDifference,
)

if TYPE_CHECKING:
    import pyarrow as pa

    from decoy_engine.execution.physical._plan import PhysicalNode
    from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
    from decoy_engine.relationships._graph import RelationshipEdge, RelationshipGraph

# The resolution logic moved to `execution._mixed_fk_bridge` so the production mixed
# route shares it without importing `execution.physical`; the shadow keeps its old name.
ShadowFkResolution = FkResolution

__all__ = [
    "FkDispatch",
    "ShadowFkResolution",
    "build_fk_dispatch",
    "classify_fk_node",
    "resolve_admitted_fk_node",
    "resolve_generate_parent_fk_child",
]


def classify_fk_node(
    table: str,
    columns: tuple[str, ...],
    *,
    relationship_graph: RelationshipGraph | None,
    admitted_edges_by_child: dict[tuple[str, tuple[str, ...]], RelationshipEdge],
) -> RelationshipEdge | None:
    """Classify one mask-table work node against the run-scoped admitted-edge
    allowlist: the admitted edge (resolve via FK), `None` (mask normally --
    not an FK child at all), or a coded decline (an FK-child node that is NOT
    on the allowlist -- e.g. a second crossing edge the admission gate should
    already have rejected upfront). `relationship_graph` is optional so this
    stays callable from a plain mask job that never sets it (every existing
    scalar/chunked/faker caller); such a job can never carry an admitted edge
    either (`admitted_edges_by_child` is empty), so this always returns
    `None` for it, unchanged.

    Never infers admission from `relationship_graph.parents_of` alone (Codex
    5b-ii plan-gate correction #4): the allowlist lookup runs FIRST, and only
    a MISS falls through to the graph-derived "is this an unadmitted FK
    child" defensive check.
    """
    key = (table, columns)
    admitted = admitted_edges_by_child.get(key)
    if admitted is not None:
        return admitted
    if relationship_graph is None:
        return None
    if relationship_graph.parents_of(table, columns):
        # Defense in depth: the admission gate's complete-graph rule already
        # guarantees no other FK-child node reaches a coupled mixed
        # dispatch, so this is normally unreachable -- but a coordinator that
        # silently masked an unadmitted FK child (ignoring its parent
        # entirely) would be a real correctness gap, not a theoretical one,
        # so this stays a coded total guard rather than a bare assumption.
        raise ShadowDifference(
            code=MIXED_FK_UNADMITTED_CHILD,
            detail=f"table={table!r}: FK-child node is not on the admitted-edge allowlist",
        )
    return None


@dataclass(frozen=True)
class FkDispatch:
    """Run-scoped FK dispatch context (slice 5b-ii): bundles the mixed
    dispatch's snapshot, relationship graph, and admitted-edge allowlist so
    the coordinator's per-node loop passes ONE object per call instead of
    three separate parameters -- built once per `run()` call, before the
    per-table loop, never per node."""

    snapshot: ShadowSnapshot
    relationship_graph: RelationshipGraph | None
    admitted_edges_by_child: dict[tuple[str, tuple[str, ...]], RelationshipEdge]


def build_fk_dispatch(
    snapshot: ShadowSnapshot,
    relationship_graph: RelationshipGraph | None,
    admitted_edges: tuple[RelationshipEdge, ...],
) -> FkDispatch:
    """Factory for `FkDispatch`, keyed like `RelationshipGraph.parents_of`
    scopes an edge -- (child_table, child_columns) -- so the coordinator's
    per-node lookup is a single dict hit, never a graph re-walk."""
    by_child = {(edge.child_table, edge.child_columns): edge for edge in admitted_edges}
    return FkDispatch(snapshot, relationship_graph, by_child)


def resolve_admitted_fk_node(
    node: PhysicalNode,
    table: str,
    source: pa.Table,
    already_assembled: dict[str, pa.Array],
    fk: FkDispatch,
) -> ShadowFkResolution | None:
    """The coordinator's per-node loop's single call point for FK handling:
    classify `node` (see `classify_fk_node`) and, only if it is the
    allowlisted edge, resolve it -- `None` means "not an FK child, mask
    normally", matching the loop's own not-an-FK-node fallthrough.
    `already_assembled` is the loop's own in-progress `columns` dict, so a
    node whose column a prior node already wrote (a duplicate declaration)
    is caught here identically to the loop's own scalar-path check."""
    edge = classify_fk_node(
        table,
        node.columns,
        relationship_graph=fk.relationship_graph,
        admitted_edges_by_child=fk.admitted_edges_by_child,
    )
    if edge is None:
        return None
    column = node.columns[0]
    if column in already_assembled:
        raise ShadowDifference(
            code=DUPLICATE_NODE_DECLARATION,
            detail=f"{table}: duplicate node_id {node.node_id!r}",
        )
    return resolve_generate_parent_fk_child(
        edge, parent_table=fk.snapshot.tables[edge.parent_table], child_table=source
    )

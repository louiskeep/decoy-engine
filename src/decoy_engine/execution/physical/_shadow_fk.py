"""Task 4.6 slice 5b-ii: shadow-side FK resolution for the ONE admitted
generate-parent -> mask-child edge a mixed dispatch's admission gate
(`_shadow_mixed._select_admitted_coupled_edge`) allowlisted.

The oracle's pool read for this shape (`_pipeline.py`'s Step 1/Step 2, see
its module docstring): a generate parent is never masked, so its pandas
source->masked map is the IDENTITY map (source ==
masked, `_pandas_adapter.py::_parent_map`, `src_series` falls back to the
never-mutated frame). This module reproduces that identity map from the
generate parent's materialized Arrow output directly (no pandas parent-side
round trip needed -- the 5b-i round-trip-stability gate already proves that
round trip is the identity for every admitted generate shape), then resolves
the child's FK column through the SAME shared `resolve_fk_keys` policy engine
the oracle uses (`execution._fk_resolve`, extracted in this slice for exactly
this reuse) so the map-hit/orphan/REMAP precedence cannot drift between the
two implementations.

Byte parity for the RESOLVED CHILD column needs one more step than the map
itself: the oracle writes a resolved FK column back through the lossless
pandas<->Arrow boundary (`_fk_keys.py`'s `to_pandas_fk_safe` ingestion +
`lossless_fk_int_values`/`fk_nullable_int_array`/`fk_all_null_array`
write-back), not a bare list assignment -- a null-bearing integer column
built the naive way silently widens to float64 and rounds any key past
2**53. `_write_back_fk_column` below reproduces that exact bridge over a
one-column frame, so the resolved column matches the oracle's dtype/values
byte-for-byte regardless of key width, nulls, or an all-null result (Codex
5b-ii plan-gate correction #3 -- do NOT gate on `> 2**53`; reproduce the
write-back instead).

Scope: single-column (non-composite) int/string keys only, matching this
slice's admission gate. A REMAP orphan against a generate parent always
fails identically to the oracle's `orphan_remap_parent_missing` (a generate
table is never a masked `WorkNode`, so `make_remap_fn`'s own parent-node
lookup always misses for this shape) -- `_reject_remap` below raises the
identical coded rejection rather than importing the pandas-bound
`make_remap_fn` closure (which this module structurally cannot call: it has
no `StrategyContext`/`StrategyHandler` to give it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import (
    fk_all_null_array,
    fk_key_value,
    fk_nullable_int_array,
    lossless_fk_int_values,
    to_pandas_fk_safe,
)
from decoy_engine.execution._fk_resolve import resolve_fk_keys
from decoy_engine.execution.physical._shadow_diff_codes import (
    DUPLICATE_NODE_DECLARATION,
    MIXED_FK_UNADMITTED_CHILD,
    ShadowDifference,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from decoy_engine.execution.physical._plan import PhysicalNode
    from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.relationships._graph import RelationshipEdge, RelationshipGraph

_KeyTuple = tuple[object, ...]

__all__ = [
    "FkDispatch",
    "ShadowFkResolution",
    "build_fk_dispatch",
    "classify_fk_node",
    "resolve_admitted_fk_node",
    "resolve_generate_parent_fk_child",
]


@dataclass(frozen=True)
class ShadowFkResolution:
    """One admitted FK child's resolved output column plus any WARN
    diagnostics the orphan policy emitted (aggregated, one per edge -- see
    `execution._fk_resolve.resolve_fk_keys`)."""

    column: pa.Array
    warnings: tuple[QualityWarning, ...]


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


def resolve_generate_parent_fk_child(
    edge: RelationshipEdge, *, parent_table: pa.Table, child_table: pa.Table
) -> ShadowFkResolution:
    """Resolve one admitted single-column generate-parent -> mask-child FK
    edge: build the parent's identity map, run it through the shared orphan-
    policy engine, and write the resolved column back through the oracle's
    own lossless pandas<->Arrow boundary."""
    parent_column = edge.parent_columns[0]
    child_column = edge.child_columns[0]
    parent_map = _identity_parent_map(parent_table, parent_column)
    child_keys = _child_key_tuples(child_table, child_column)
    masked_keys, warnings, _cascade = resolve_fk_keys(
        child_keys,
        parent_map,
        edge,
        remap_fn=_reject_remap(edge),
        # A generate parent is never row-errored (quarantine is one of this
        # slice's admission-gate exclusions), so there is nothing to cascade.
        errored_parent_keys=None,
    )
    values: list[object] = [None if key is None else key[0] for key in masked_keys]
    column = _write_back_fk_column(child_table, child_column, values)
    return ShadowFkResolution(column=column, warnings=tuple(warnings))


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


def _identity_parent_map(parent_table: pa.Table, parent_column: str) -> dict[_KeyTuple, _KeyTuple]:
    """The generate parent's source-key -> masked-key map, reproduced as an
    IDENTITY map with the oracle's exact normalization (`_pandas_adapter.py
    ::_parent_map`, see this module's docstring for why identity is correct
    here): a parent row with a null key contributes nothing (an unreferenced
    key, matching the oracle's own null-key skip); every other row's key
    normalizes via the shared `fk_key_value` for the map LOOKUP, while the
    mapped VALUE preserves the parent's own original value, unnormalized (an
    orphan-preserved or matched child must read back the exact source type/
    value, not a normalized stand-in). Row order iterates the table's own
    order, so a duplicate key's LAST row wins -- the same last-write-wins a
    plain Python dict assignment gives the oracle's row-by-row loop.
    """
    out: dict[_KeyTuple, _KeyTuple] = {}
    for value in parent_table.column(parent_column).to_pylist():
        if value is None:
            continue
        out[(fk_key_value(value),)] = (value,)
    return out


def _child_key_tuples(child_table: pa.Table, child_column: str) -> list[_KeyTuple | None]:
    """One key tuple per child row (`None` for a null FK, preserved as null
    and never an orphan -- matches `_pandas_adapter.py::_resolve_fk_node`'s
    own null-key handling). Arrow's own `to_pylist()` already returns exact
    Python ints for an int64-with-nulls column (Arrow has no float64-on-null
    promotion the way pandas does), so no FK-safe pandas ingestion is needed
    on this READ side -- only the WRITE-back side needs the lossless bridge
    (see `_write_back_fk_column`)."""
    return [
        None if value is None else (fk_key_value(value),)
        for value in child_table.column(child_column).to_pylist()
    ]


def _reject_remap(edge: RelationshipEdge) -> Callable[[list[_KeyTuple]], list[_KeyTuple]]:
    """A REMAP orphan against a generate parent always fails: `make_remap_fn`
    (the oracle's own REMAP closure) mints a remapped key via the PARENT
    column's masked `WorkNode`, and a generate-kind table never has one (it
    is generate-kind, never mask-kind, so `build_work_list` never emits a
    node for it) -- the oracle's own closure raises `orphan_remap_parent_
    missing` for this shape every time, never actually remapping. This
    closure raises the IDENTICAL coded exception with the identical message,
    rather than importing the pandas-bound `make_remap_fn` this module has
    no `StrategyContext`/`StrategyHandler` to satisfy."""
    parent_table = edge.parent_table
    parent_column = edge.parent_columns[0]

    def _remap(orphan_keys: list[_KeyTuple]) -> list[_KeyTuple]:
        del orphan_keys  # unused: this edge shape always rejects, never remaps
        raise ExecutionError(
            code="orphan_remap_parent_missing",
            message=(
                f"REMAP needs the parent column {parent_table}.{parent_column} to be a "
                "masked scalar node, but it is absent from the work list."
            ),
        )

    return _remap


def _write_back_fk_column(
    child_table: pa.Table, child_column: str, values: list[object]
) -> pa.Array:
    """Materialize the resolved child FK column through the SAME pandas->
    Arrow write-back boundary `_pandas_adapter.py::_resolve_fk_node` uses
    (Codex 5b-ii plan-gate correction #3): ingest the child's own column
    FK-safely (so the pre-resolution dtype `fk_all_null_array` may fall back
    to is the oracle's exact per-Arrow-type nullable dtype, not a numpy
    default), classify the resolved values, assign back via the identical
    lossless helper, and round-trip the one-column frame through
    `pa.Table.from_pandas`. A one-column frame infers the same Arrow type
    `pa.Table.from_pandas` would give this column inside the oracle's full
    multi-column frame -- pandas->Arrow schema inference is column-local, not
    influenced by sibling columns.
    """
    one_column = pa.table({child_column: child_table.column(child_column)})
    frame = to_pandas_fk_safe(one_column, {child_column})
    safe_ints = lossless_fk_int_values(values)
    if safe_ints is not None and all(v is None for v in safe_ints):
        frame[child_column] = fk_all_null_array(len(safe_ints), frame[child_column].dtype)
    elif safe_ints is not None:
        frame[child_column] = fk_nullable_int_array(safe_ints)
    else:
        frame[child_column] = values
    resolved = pa.Table.from_pandas(frame, preserve_index=False)
    return resolved.column(child_column).combine_chunks()

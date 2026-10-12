"""Pre-load shape checks for the sequential (SEQ) route.

REMAP mints an orphan key by re-running the PARENT column's own scalar handler
(`_strategies/_orphan.py::make_remap_fn`), so it needs a scalar work node for every parent key
column. A composite child collapses its member columns into one `composite_fk_group` node and
the planner drops them from `per_column` (`plan/_seed_envelope.py`), so a composite child that
is itself the parent of a later REMAP edge (`A.(k,j) -> B.(k,j) -> C.(k,j)`) has none. The
full-frame oracle fails the same shape with the raw `orphan_remap_parent_missing`.

Phase D keeps that shape unsupported and says so up front, before any table loads. The test is
by MEMBER: a REMAP edge is declined when any of its parent columns is a member of a composite
group on the parent table and has no scalar node of its own. Comparing whole tuples would miss
a reordered or partly overlapping group. Only a REMAP edge is declined: the other orphan
policies never re-run the parent handler, and they work on this shape today.

The error names schema identifiers (table and column names) and nothing from the data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable

    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.relationships._graph import RelationshipGraph

COMPOSITE_CHAIN_CODE = "sequential_composite_fk_chain_unsupported"


def check_composite_fk_chain(nodes: Iterable[WorkNode], graph: RelationshipGraph) -> None:
    """Raise `sequential_composite_fk_chain_unsupported` for a REMAP edge that cannot re-derive."""
    group_members: dict[str, set[str]] = {}
    scalar_columns: dict[str, set[str]] = {}
    for node in nodes:
        if node.kind == "composite_fk_group":
            group_members.setdefault(node.table, set()).update(node.columns)
        elif node.kind == "scalar" and isinstance(node.plan_slice, ColumnSeed):
            scalar_columns.setdefault(node.table, set()).update(node.columns)
    for edge in graph.edges:
        if edge.orphan_policy is not OrphanPolicy.REMAP:
            continue
        table = edge.parent_table
        missing = [
            c
            for c in edge.parent_columns
            if c in group_members.get(table, ()) and c not in scalar_columns.get(table, ())
        ]
        if missing:
            raise ExecutionError(
                code=COMPOSITE_CHAIN_CODE,
                message=(
                    f"table {edge.child_table!r} references composite key columns "
                    f"{', '.join(map(repr, missing))} of table {table!r} with orphan policy "
                    "'remap', but those columns are themselves part of a composite foreign key. "
                    "The sequential route cannot re-derive orphan keys through a composite "
                    "foreign-key chain; use another orphan policy or a single-column key."
                ),
            )

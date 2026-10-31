"""Clean decline for REMAP through a composite foreign-key chain on the sequential (SEQ) route.

REMAP mints an orphan key by re-running the PARENT column's own scalar handler
(`_strategies/_orphan.py::make_remap_fn`), so it needs a scalar work node for every parent key
column. A composite child collapses its member columns into one `composite_fk_group` node and
the planner drops them from `per_column` (`plan/_seed_envelope.py`), so a composite child that
is itself the parent of a later REMAP edge (`A.(k,j) -> B.(k,j) -> C.(k,j)`) has none. A genuine
orphan on that hop raises the raw `orphan_remap_parent_missing` on the full-frame oracle and on
SEQ alike. A chain whose keys all match never reaches REMAP and runs fine, so nothing here
looks at the shape until that exception fires.

On SEQ only, the dispatch wrapper below turns that exception into a fixed, coded decline. It
keeps the shared orphan site and the full-frame behavior untouched. The translation applies only
when the failing table has a REMAP edge whose parent columns are members of a composite group on
the parent table with no scalar node of their own. The test is by MEMBER, since comparing whole
tuples would miss a reordered or partly overlapping group. The message names schema identifiers
(table and column names) and nothing from the data, and carries no cause chain.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.relationships._graph import RelationshipEdge, RelationshipGraph

COMPOSITE_CHAIN_CODE = reasons.runtime.SEQUENTIAL_COMPOSITE_FK_CHAIN_UNSUPPORTED
_RAW_CODE = reasons.runtime.ORPHAN_REMAP_PARENT_MISSING


def composite_chain_offender(
    nodes: Iterable[WorkNode], graph: RelationshipGraph, child_table: str | None = None
) -> tuple[RelationshipEdge, list[str]] | None:
    """The first REMAP edge (into `child_table`, when given) that cannot re-derive, with the
    parent columns it lacks scalar nodes for; None when every REMAP edge can."""
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
        if child_table is not None and edge.child_table != child_table:
            continue
        table = edge.parent_table
        missing = [
            c
            for c in edge.parent_columns
            if c in group_members.get(table, ()) and c not in scalar_columns.get(table, ())
        ]
        if missing:
            return edge, missing
    return None


@contextmanager
def decline_composite_chain(
    nodes: Iterable[WorkNode], graph: RelationshipGraph, child_table: str
) -> Iterator[None]:
    """Re-raise the raw missing-parent error as `sequential_composite_fk_chain_unsupported`
    when `child_table`'s REMAP edge hits a composite chain; any other error passes through."""
    try:
        yield
    except ExecutionError as exc:
        if exc.code != _RAW_CODE:
            raise
        found = composite_chain_offender(nodes, graph, child_table)
        if found is None:
            raise
        edge, missing = found
        raise ExecutionError(
            code=COMPOSITE_CHAIN_CODE,
            message=(
                f"table {edge.child_table!r} has orphan keys against composite key columns "
                f"{', '.join(map(repr, missing))} of table {edge.parent_table!r} under orphan "
                "policy 'remap', but those columns are themselves part of a composite foreign "
                "key. The sequential route cannot re-derive orphan keys through a composite "
                "foreign-key chain; use another orphan policy or a single-column key."
            ),
        ) from None

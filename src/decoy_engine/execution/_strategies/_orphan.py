"""Orphan-FK policy resolution for the pandas execution adapter (engine-v2 S9 slice 2h).

A child FK column references a parent key. Its masked value is the PARENT's
masked value for the same source key, looked up through the in-run parent
source->masked map the runner builds as it masks parents (referential integrity
by construction, not by re-derive coincidence). A child row whose source key has
no parent is an ORPHAN, handled per the edge's `OrphanPolicy` (cross-sprint
contracts row 7; S9 spec 6.2):

- `PRESERVE`: keep the original source key (unmasked).
- `REMAP`: assign a fresh masked key via the parent column's strategy (so a
  remapped orphan is indistinguishable from a normally-masked value).
- `WARN`: PRESERVE behavior + one AGGREGATED `QualityWarning(code='orphan_fk')`
  per edge (never one-per-row: a 100k-row child must not emit 100k warnings).
- `FAIL`: raise `ExecutionError(code='orphan_fk_violation')`.

Keys are tuples (a single-column FK is a 1-tuple, a composite FK an N-tuple), so
the same resolver serves scalar FK children and composite-FK group nodes. S9
honors the policy; S10 reports it.
"""

from __future__ import annotations

from collections.abc import Callable

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

_KeyTuple = tuple[object, ...]


def resolve_fk_keys(
    child_keys: list[_KeyTuple | None],
    parent_map: dict[_KeyTuple, _KeyTuple],
    edge: RelationshipEdge,
    *,
    remap_fn: Callable[[list[_KeyTuple]], list[_KeyTuple]],
) -> tuple[list[_KeyTuple | None], list[QualityWarning]]:
    """Map each child source key to its masked key, applying the orphan policy.

    `child_keys` carries one entry per child row: `None` for a null FK (preserved
    as null, never an orphan), else the row's source key tuple. Returns the masked
    key per row (None where the input was None) plus any aggregated warnings.
    """
    masked: list[_KeyTuple | None] = [None] * len(child_keys)
    orphan_positions: list[int] = []
    orphan_keys: list[_KeyTuple] = []
    for i, key in enumerate(child_keys):
        if key is None:
            continue  # null FK: preserved as null
        mapped = parent_map.get(key)
        if mapped is not None:
            masked[i] = mapped
            continue
        orphan_positions.append(i)
        orphan_keys.append(key)

    if not orphan_positions:
        return masked, []

    policy = edge.orphan_policy
    if policy is OrphanPolicy.FAIL:
        raise ExecutionError(
            code="orphan_fk_violation",
            message=(
                f"{len(orphan_positions)} orphan row(s) in "
                f"{edge.child_table}.{edge.child_columns} reference no parent key in "
                f"{edge.parent_table}.{edge.parent_columns} (orphan_policy=fail)."
            ),
        )

    if policy is OrphanPolicy.REMAP:
        remapped = remap_fn(orphan_keys)
        for pos, val in zip(orphan_positions, remapped, strict=True):
            masked[pos] = val
        return masked, []

    # PRESERVE and WARN both keep the source key unmasked.
    for pos, key in zip(orphan_positions, orphan_keys, strict=True):
        masked[pos] = key
    warnings: list[QualityWarning] = []
    if policy is OrphanPolicy.WARN:
        warnings.append(
            QualityWarning(
                code="orphan_fk",
                provider=edge.namespace,
                column=",".join(edge.child_columns),
                detail={
                    "parent_table": edge.parent_table,
                    "parent_columns": list(edge.parent_columns),
                    "child_table": edge.child_table,
                    "child_columns": list(edge.child_columns),
                    "orphan_rows": len(orphan_positions),
                },
            )
        )
    return masked, warnings

"""fk_validity scan (engine-v2 S10): masked child FKs resolve to masked parents.

For every relationship edge, the masked child FK key (single value or composite
tuple) should appear in the masked parent PK set -- that is the referential
integrity S9's FK resolver preserves. A non-null child key with no parent match is
an ORPHAN (expected under PRESERVE/WARN/REMAP; under FAIL the job would have raised
at execution, so a post-hoc unmatched row is INVALID). Per `OrphanPolicy`: FAIL ->
hard fail on any invalid; WARN -> a warning; PRESERVE/REMAP -> pass. Produces one
`FkValidityReport` per relationship (cross-sprint contracts §3 row 8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.relationships._graph import OrphanPolicy
from decoy_engine.validation.post._scan import ScanContext, ScanOutcome, column_values
from decoy_engine.validation.post._types import FkValidityReport

if TYPE_CHECKING:
    import pyarrow as pa

_NAME = "fk_validity"


def run_fk_validity(ctx: ScanContext) -> ScanOutcome:
    reports: dict[str, FkValidityReport] = {}
    warnings: list[QualityWarning] = []
    failed = False

    for edge in ctx.relationship_graph.edges:
        out_child = ctx.outputs.get(edge.child_table)
        if out_child is None:
            continue
        relationship = (
            f"{edge.parent_table}.{','.join(edge.parent_columns)} -> "
            f"{edge.child_table}.{','.join(edge.child_columns)}"
        )
        parent_keys = _key_set(ctx, edge.parent_table, edge.parent_columns)
        child_keys = _row_keys(out_child, edge.child_columns)

        child_row_count = len(child_keys)
        parent_match = 0
        orphan = 0
        for key in child_keys:
            if key is None:
                continue  # null FK: not a match, not an orphan
            if key in parent_keys:
                parent_match += 1
            else:
                orphan += 1

        policy = edge.orphan_policy
        invalid = orphan if policy is OrphanPolicy.FAIL else 0
        reports[relationship] = FkValidityReport(
            relationship=relationship,
            namespace=edge.namespace,
            orphan_policy=policy.value,
            child_row_count=child_row_count,
            parent_match_count=parent_match,
            orphan_count=orphan,
            invalid_count=invalid,
        )
        if policy is OrphanPolicy.FAIL and invalid > 0:
            failed = True
        elif policy is OrphanPolicy.WARN and orphan > 0:
            warnings.append(
                QualityWarning(
                    code="orphan_fk",
                    provider=edge.namespace,
                    column=",".join(edge.child_columns),
                    detail={"relationship": relationship, "orphan_count": orphan},
                )
            )

    return ScanOutcome(name=_NAME, failed=failed, fk_validity=reports, warnings=tuple(warnings))


def _key_set(ctx: ScanContext, table: str, columns: tuple[str, ...]) -> set[tuple[object, ...]]:
    out_table = ctx.outputs.get(table)
    if out_table is None:
        return set()
    keys = _row_keys(out_table, columns)
    return {k for k in keys if k is not None}


def _row_keys(table: pa.Table, columns: tuple[str, ...]) -> list[tuple[object, ...] | None]:
    col_values = [column_values(table, c) for c in columns]
    if not col_values:
        return []
    n = len(col_values[0])
    keys: list[tuple[object, ...] | None] = []
    for i in range(n):
        row = tuple(col_values[j][i] for j in range(len(columns)))
        keys.append(None if any(x is None for x in row) else row)
    return keys

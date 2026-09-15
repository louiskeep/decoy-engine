"""Task 4.4 C5: `ShadowSnapshot` -- the resident in-memory tables the shadow
coordinator consumes, plus a non-sensitive snapshot-identity digest.

This is a test-harness-facing capture over immutable `pa.Table` objects.
`capture_shadow_snapshot` receives the SAME resident object the oracle's test
also consumes, so both sides see byte-identical input by construction.
`snapshot_identity` records a proof of that identity for test validation.

Task 4.5 activates the ShadowCoordinator (that uses this snapshot) as the
production engine for the unified-slice lane (bounded non-FK single-table masks
on already-resident sources). The production lane itself builds PhysicalPlanInputs
via `_live_inputs.py` (from run_pipeline facts), not via `_snapshot.py`
(which remains the shadow/test builder), so production readers never re-profile
or re-plan against a source.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

import pyarrow as pa

__all__ = ["ShadowSnapshot", "capture_shadow_snapshot", "snapshot_identity"]


@dataclass(frozen=True)
class ShadowSnapshot:
    """The resident tables the shadow coordinator reads. `tables` is copied
    into a fresh `dict` at construction, so a caller mutating its own
    mapping afterward cannot change what the coordinator sees."""

    tables: Mapping[str, pa.Table]

    def identity(self, table: str) -> str:
        return snapshot_identity(self.tables[table])


def snapshot_identity(table: pa.Table) -> str:
    """A non-sensitive digest of exactly what one side consumed (C5):
    ordered schema, row count, per-column null validity, and row-ordered
    values -- hashed, never returned as a raw cell dump. Two calls over the
    same resident `pa.Table` object are equal by construction; recording
    this per side proves that identity rather than establishing it (the
    harness passes the identical object to both the shadow snapshot and the
    oracle's `sources` mapping -- see this module's docstring).
    """
    digest = hashlib.sha256()
    digest.update(repr(table.schema).encode("utf-8"))
    digest.update(str(table.num_rows).encode("utf-8"))
    for name in table.column_names:
        column = table.column(name)
        digest.update(name.encode("utf-8"))
        digest.update(repr(column.is_valid().to_pylist()).encode("utf-8"))
        digest.update(repr(column.to_pylist()).encode("utf-8"))
    return digest.hexdigest()


def capture_shadow_snapshot(tables: Mapping[str, pa.Table]) -> ShadowSnapshot:
    return ShadowSnapshot(tables=dict(tables))

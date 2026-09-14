"""Task 4.4 C5: `ShadowSnapshot` -- the resident, in-memory input the shadow
side profiles, plus its non-sensitive snapshot-identity digest.

Scope decision (TASK-4.4-PLAN.md C5): this is a shadow-test-scoped capture
over an immutable Parquet fixture, not a production single-open seam (that
is deferred to Task 4.5, when the coordinator becomes the real production
reader of a possibly-mutable source). `capture_shadow_snapshot` is handed
the SAME resident `pa.Table` object the test harness also passes to the
oracle's `run_pipeline(sources=...)` call, so both sides consume
byte-identical input by construction; `snapshot_identity` is the recorded
proof of that, not the mechanism that makes it true.
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

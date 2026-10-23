"""The ``Dialect`` seam for the DuckDB-attach connectors (D0).

A dialect supplies the ~6 things that differ per source database; the core
orchestration (`_core.run_snapshot`) supplies everything else (dir layout,
deadline, transaction, atomic publish, cleanup). Adding a source = implementing
this protocol + its fidelity matrix + its consistency qualification.

Hooks:
- ``log_label`` / ``attach_alias``: the message prefix and the fixed ATTACH alias.
- ``error``: build the dialect's coded, redacted ``SnapshotError`` subclass.
- ``open_connection``: a configured in-memory DuckDB connection (offline, bounded).
- ``load_extension``: LOAD + verify the bundled scanner extension (fail closed offline).
- ``pre_attach``: dialect SET statements issued before ATTACH (e.g. statement timeout).
- ``attach``: the ``ATTACH … READ_ONLY`` statement, carrying credentials safely.
- ``check_types``: read declared types and return a per-column guard class, or reject.
- ``guard_values``: reject values the scanner would alter silently, before export.
- ``export_relation``: the deterministic ``COPY (SELECT … ORDER BY …) TO parquet``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from decoy_engine.connectors.duckdb_attach._models import RelationSelection, SnapshotConfig


@runtime_checkable
class Dialect(Protocol):
    log_label: str
    attach_alias: str

    def error(self, code: str, *, location: str = "", cause_type: str = "") -> Exception: ...

    def open_connection(self, config: SnapshotConfig, spill_dir: Path) -> Any: ...

    def load_extension(self, conn: Any, config: SnapshotConfig) -> None: ...

    def pre_attach(self, conn: Any, config: SnapshotConfig) -> None: ...

    def attach(self, conn: Any, config: SnapshotConfig, alias: str) -> None: ...

    def check_types(self, conn: Any, sel: RelationSelection, alias: str) -> dict[str, str]: ...

    def guard_values(
        self, conn: Any, sel: RelationSelection, classes: dict[str, str], alias: str
    ) -> None: ...

    def export_relation(
        self, conn: Any, sel: RelationSelection, target: Path, row_group_size: int, alias: str
    ) -> None: ...

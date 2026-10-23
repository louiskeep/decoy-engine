"""DuckDB-file source adapter (DuckDB sources D2).

A ``.duckdb`` database file attached read-only. It is a *local file* source -- no
scanner extension, no network, no credentials -- and its types are native
DuckDB types, so every column is carried losslessly (no value guard, no
preserve-or-reject tier). This is the simplest dialect and the one fully
exercised in-sandbox; it also stands as the worked reference for the scanner
dialects.

The path is taken as given (resolved by the caller). Confining it to an
operator-approved root is the platform's fence (the library does not know its
callers); this adapter attaches the path read-only and reads catalog + rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from decoy_engine.connectors.duckdb_attach._errors import SnapshotError
from decoy_engine.connectors.duckdb_attach._models import RelationSelection, SnapshotConfig
from decoy_engine.connectors.duckdb_attach._sql import _lit, _q, export_relation

_ATTACH_ALIAS = "decoy_duckdb_src"


class DuckDbFileError(SnapshotError):
    """Coded, redacted failure. Message: ``duckdb_file_snapshot[code] …``."""

    _PREFIX = "duckdb_file_snapshot"


class DuckDbFileDialect:
    """Read a selection of relations from an attached ``.duckdb`` file."""

    log_label = "duckdb_file_snapshot"
    attach_alias = _ATTACH_ALIAS

    def error(self, code: str, *, location: str = "", cause_type: str = "") -> DuckDbFileError:
        return DuckDbFileError(code, location=location, cause_type=cause_type)

    def open_connection(self, config: SnapshotConfig, spill_dir: Path) -> Any:
        import os

        import duckdb

        spill_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(spill_dir, 0o700)
        try:
            return duckdb.connect(
                database=":memory:",
                config={
                    "temp_directory": str(spill_dir),
                    "memory_limit": config.memory_limit,
                    "threads": config.threads,
                    # No extension is loaded, but keep the offline fence on so a
                    # stray autoload can never reach the network.
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                    "allow_unsigned_extensions": False,
                    "preserve_insertion_order": True,
                },
            )
        except Exception as exc:
            raise DuckDbFileError("duckdb_unavailable", cause_type=type(exc).__name__) from None

    def load_extension(self, conn: Any, config: SnapshotConfig) -> None:
        return None  # TYPE duckdb is built in

    def pre_attach(self, conn: Any, config: SnapshotConfig) -> None:
        return None  # no statement-timeout knob; the run's deadline interrupts

    def attach(self, conn: Any, config: SnapshotConfig, alias: str) -> None:
        conn.execute(f"ATTACH {_lit(config.dsn.get_secret_value())} AS {_q(alias)} (READ_ONLY)")

    def _columns(self, conn: Any, sel: RelationSelection, alias: str) -> dict[str, str]:
        try:
            rows = conn.execute(
                "SELECT column_name, data_type FROM duckdb_columns() "
                "WHERE database_name = ? AND schema_name = ? AND table_name = ?",
                [alias, sel.schema_name, sel.table],
            ).fetchall()
        except Exception as exc:
            raise DuckDbFileError(
                "catalog_read_failed", location=sel.key, cause_type=type(exc).__name__
            ) from None
        if not rows:
            raise DuckDbFileError("relation_not_found", location=sel.key)
        folded: dict[str, int] = {}
        for name, _ in rows:
            folded[name.lower()] = folded.get(name.lower(), 0) + 1
        if any(count > 1 for count in folded.values()):
            # Same case-collision hazard the Postgres adapter rejects: DuckDB
            # folds a colliding pair and could export the wrong column silently.
            raise DuckDbFileError("ambiguous_name", location=sel.key)
        return {name: dtype for name, dtype in rows}

    def check_types(self, conn: Any, sel: RelationSelection, alias: str) -> dict[str, str]:
        declared = self._columns(conn, sel, alias)
        classes: dict[str, str] = {}
        for col in sel.columns:
            if col not in declared:
                raise DuckDbFileError("column_not_found", location=f"{sel.key}.{col}")
            classes[col] = "plain"  # native DuckDB type: lossless, no guard
        return classes

    def guard_values(
        self, conn: Any, sel: RelationSelection, classes: dict[str, str], alias: str
    ) -> None:
        return None  # native types: nothing is silently altered

    def export_relation(
        self, conn: Any, sel: RelationSelection, target: Path, row_group_size: int, alias: str
    ) -> None:
        export_relation(conn, sel, target, row_group_size, alias)

    def declared_types(
        self, conn: Any, alias: str
    ) -> dict[tuple[str, str, str], tuple[str, str]]:
        rows = conn.execute(
            "SELECT schema_name, table_name, column_name, data_type FROM duckdb_columns() "
            "WHERE database_name = ?",
            [alias],
        ).fetchall()
        return {(r[0], r[1], r[2]): (r[3], "") for r in rows}

    def column_verdict(self, declared_type: str, typtype: str) -> tuple[str, str]:
        return "supported", "native"  # a .duckdb file IS DuckDB: lossless


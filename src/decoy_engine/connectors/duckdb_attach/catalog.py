"""Uniform catalog browser for any DuckDB-attached source (DuckDB sources D1).

The exploration surface: attach a source read-only and list its schemas,
tables, and columns, each column annotated with DuckDB's mapped type, the
source's declared type, and a fidelity verdict (the preserve/reject/lossy call
the snapshot would make). Enumeration is dialect-independent -- DuckDB exposes
every attached catalog through ``duckdb_tables()`` / ``duckdb_columns()`` -- so
a dialect supplies only the declared-type read and the per-column verdict.

No data rows are read; this reads catalog metadata only. Bounded by the
config's deadline; errors are the dialect's coded, redacted ``SnapshotError``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from decoy_engine.connectors.duckdb_attach._dialect import Dialect
from decoy_engine.connectors.duckdb_attach._errors import SnapshotError
from decoy_engine.connectors.duckdb_attach._models import SnapshotConfig

# Catalog metadata for the attached database, filtered to the attach alias.
_TABLES_SQL = (
    "SELECT schema_name, table_name FROM duckdb_tables() "
    "WHERE database_name = ? ORDER BY schema_name, table_name"
)
_COLUMNS_SQL = (
    "SELECT schema_name, table_name, column_name, data_type FROM duckdb_columns() "
    "WHERE database_name = ? ORDER BY schema_name, table_name, column_index"
)


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    duckdb_type: str  # DuckDB's mapped type (from duckdb_columns)
    source_type: str  # the source's declared type (from the dialect), or "" if unknown
    verdict: str  # "supported" | "lossy" | "rejected"
    note: str  # guard class (supported), caveat (lossy), or reason (rejected)


@dataclass(frozen=True)
class TableInfo:
    schema: str
    table: str
    columns: tuple[ColumnInfo, ...] = ()


@dataclass(frozen=True)
class CatalogListing:
    schemas: tuple[str, ...] = ()
    tables: tuple[TableInfo, ...] = ()


def _assemble_listing(
    table_rows: list[tuple[str, str]],
    column_rows: list[tuple[str, str, str, str]],
    declared: dict[tuple[str, str, str], tuple[str, str]],
    dialect: Dialect,
) -> CatalogListing:
    """Pure assembly: join tables + columns + declared types into a listing with
    a per-column verdict. Kept free of I/O so it is directly unit-testable."""
    cols_by_rel: dict[tuple[str, str], list[ColumnInfo]] = {}
    for schema_name, table_name, column_name, duckdb_type in column_rows:
        decl, typtype = declared.get((schema_name, table_name, column_name), ("", ""))
        if decl:
            verdict, note = dialect.column_verdict(decl, typtype)
        else:
            # No declared type from the source catalog for this column: cannot
            # vouch for fidelity, so do not claim "supported".
            verdict, note = "rejected", "declared_type_unavailable"
        cols_by_rel.setdefault((schema_name, table_name), []).append(
            ColumnInfo(
                name=column_name,
                duckdb_type=duckdb_type,
                source_type=decl,
                verdict=verdict,
                note=note,
            )
        )
    tables = tuple(
        TableInfo(schema=s, table=t, columns=tuple(cols_by_rel.get((s, t), [])))
        for s, t in table_rows
    )
    schemas = tuple(dict.fromkeys(s for s, _ in table_rows))  # order-stable distinct
    return CatalogListing(schemas=schemas, tables=tables)


def browse(config: SnapshotConfig, *, dialect: Dialect) -> CatalogListing:
    """Attach the source read-only and return its schemas/tables/columns with
    fidelity verdicts. Metadata only; bounded by ``config.deadline_s``."""
    import uuid

    alias = dialect.attach_alias
    spill_dir = config.temp_directory / f"browse-{uuid.uuid4().hex}"
    conn: Any = None
    timed_out = threading.Event()
    timer: threading.Timer | None = None
    try:
        conn = dialect.open_connection(config, spill_dir)
        dialect.load_extension(conn, config)

        def _on_deadline() -> None:
            timed_out.set()
            try:
                conn.interrupt()
            except Exception:
                pass

        timer = threading.Timer(config.deadline_s, _on_deadline)
        timer.daemon = True
        timer.start()

        try:
            conn.execute("SET TimeZone = 'UTC'")
            dialect.pre_attach(conn, config)
            dialect.attach(conn, config, alias)
        except Exception as exc:
            raise dialect.error("source_unreachable", cause_type=type(exc).__name__) from None

        try:
            table_rows = conn.execute(_TABLES_SQL, [alias]).fetchall()
            column_rows = conn.execute(_COLUMNS_SQL, [alias]).fetchall()
            declared = dialect.declared_types(conn, alias)
        except SnapshotError:
            if timed_out.is_set():
                raise dialect.error("deadline_exceeded") from None
            raise
        except Exception as exc:
            if timed_out.is_set():
                raise dialect.error("deadline_exceeded") from None
            raise dialect.error("catalog_read_failed", cause_type=type(exc).__name__) from None

        if timed_out.is_set():
            raise dialect.error("deadline_exceeded")
        return _assemble_listing(table_rows, column_rows, declared, dialect)
    finally:
        if timer is not None:
            timer.cancel()
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        import shutil

        shutil.rmtree(spill_dir, ignore_errors=True)



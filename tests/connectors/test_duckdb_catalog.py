"""Uniform catalog browser (DuckDB sources D1).

Two layers: the pure listing assembly + verdict join (no I/O, a fake dialect),
and a real browse() against a DuckDB-file attach -- which exercises the generic
duckdb_tables()/duckdb_columns() enumeration path end to end without needing a
scanner extension (the postgres/sqlite scanners are proven in CI, which has a
live server + the bundled extension).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")

from decoy_engine.connectors.duckdb_attach import SnapshotConfig, SnapshotError, browse
from decoy_engine.connectors.duckdb_attach.catalog import ColumnInfo, _assemble_listing


class _VerdictDialect:
    """Minimal dialect for the pure-assembly tests: a canned verdict per type."""

    def column_verdict(self, declared_type: str, typtype: str) -> tuple[str, str]:
        if declared_type == "bad":
            return "rejected", f"unsupported_type:{declared_type}"
        if declared_type == "iffy":
            return "lossy", "all_varchar"
        return "supported", "plain"


def test_assemble_joins_types_and_verdicts() -> None:
    tables = [("public", "orders"), ("public", "customers")]
    columns = [
        ("public", "orders", "id", "INTEGER"),
        ("public", "orders", "amt", "DECIMAL(10,2)"),
        ("public", "customers", "name", "VARCHAR"),
    ]
    declared = {
        ("public", "orders", "id"): ("integer", "b"),
        ("public", "orders", "amt"): ("numeric(10,2)", "b"),
        ("public", "customers", "name"): ("text", "b"),
    }
    listing = _assemble_listing(tables, columns, declared, _VerdictDialect())
    assert listing.schemas == ("public",)
    orders = next(t for t in listing.tables if t.table == "orders")
    assert [c.name for c in orders.columns] == ["id", "amt"]
    amt = next(c for c in orders.columns if c.name == "amt")
    assert amt.duckdb_type == "DECIMAL(10,2)" and amt.source_type == "numeric(10,2)"
    assert amt.verdict == "supported"


def test_assemble_marks_rejected_and_lossy_and_missing_declared() -> None:
    tables = [("s", "t")]
    columns = [
        ("s", "t", "ok", "VARCHAR"),
        ("s", "t", "weird", "VARCHAR"),
        ("s", "t", "softish", "VARCHAR"),
        ("s", "t", "unknown", "VARCHAR"),  # no declared entry
    ]
    declared = {
        ("s", "t", "ok"): ("text", "b"),
        ("s", "t", "weird"): ("bad", "b"),
        ("s", "t", "softish"): ("iffy", "b"),
    }
    cols = {c.name: c for c in _assemble_listing(tables, columns, declared, _VerdictDialect()).tables[0].columns}
    assert cols["ok"].verdict == "supported"
    assert cols["weird"].verdict == "rejected" and "unsupported_type" in cols["weird"].note
    assert cols["softish"].verdict == "lossy" and cols["softish"].note == "all_varchar"
    # A column the source catalog did not describe cannot be vouched for.
    assert cols["unknown"].verdict == "rejected" and cols["unknown"].note == "declared_type_unavailable"


# --- real browse() over a DuckDB-file attach --------------------------------


class _DuckDbFileError(SnapshotError):
    _PREFIX = "duckdb_file_snapshot"


class _DuckDbFileDialect:
    """A trivial read-only dialect attaching a .duckdb file: no extension needed.
    Exercises the generic enumeration/assembly in browse() against a real attach."""

    log_label = "duckdb_file_snapshot"
    attach_alias = "decoy_src"

    def error(self, code: str, *, location: str = "", cause_type: str = "") -> _DuckDbFileError:
        return _DuckDbFileError(code, location=location, cause_type=cause_type)

    def open_connection(self, config: SnapshotConfig, spill_dir: Path) -> Any:
        import duckdb

        spill_dir.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(database=":memory:")

    def load_extension(self, conn: Any, config: SnapshotConfig) -> None:
        return None

    def pre_attach(self, conn: Any, config: SnapshotConfig) -> None:
        return None

    def attach(self, conn: Any, config: SnapshotConfig, alias: str) -> None:
        conn.execute(f"ATTACH '{config.dsn.get_secret_value()}' AS \"{alias}\" (READ_ONLY)")

    def declared_types(self, conn: Any, alias: str) -> dict[tuple[str, str, str], tuple[str, str]]:
        rows = conn.execute(
            "SELECT schema_name, table_name, column_name, data_type FROM duckdb_columns() "
            "WHERE database_name = ?",
            [alias],
        ).fetchall()
        return {(r[0], r[1], r[2]): (r[3], "") for r in rows}

    def column_verdict(self, declared_type: str, typtype: str) -> tuple[str, str]:
        return "supported", "native"  # a duckdb file IS duckdb: lossless


def _config(path: Path, tmp_path: Path) -> SnapshotConfig:
    return SnapshotConfig(
        dsn=str(path),
        extension_directory=tmp_path / "ext",
        memory_limit="256MB",
        temp_directory=tmp_path / "spill",
        threads=1,
        row_group_size=1000,
        statement_timeout_s=5,
        deadline_s=20,
    )


def test_browse_enumerates_a_real_attached_catalog(tmp_path: Path) -> None:
    import duckdb

    src = tmp_path / "src.duckdb"
    con = duckdb.connect(str(src))
    con.execute("CREATE TABLE orders(id INTEGER, amt DOUBLE, note VARCHAR)")
    con.execute("CREATE TABLE customers(cid INTEGER, name VARCHAR)")
    con.close()

    listing = browse(_config(src, tmp_path), dialect=_DuckDbFileDialect())

    assert set(listing.schemas) == {"main"}
    assert {(t.schema, t.table) for t in listing.tables} == {("main", "orders"), ("main", "customers")}
    orders = next(t for t in listing.tables if t.table == "orders")
    assert [(c.name, c.duckdb_type) for c in orders.columns] == [
        ("id", "INTEGER"),
        ("amt", "DOUBLE"),
        ("note", "VARCHAR"),
    ]
    assert all(c.verdict == "supported" and c.source_type for c in orders.columns)


def test_browse_source_unreachable_is_coded(tmp_path: Path) -> None:
    missing = tmp_path / "nope.duckdb"
    with pytest.raises(SnapshotError) as ei:
        browse(_config(missing, tmp_path), dialect=_DuckDbFileDialect())
    assert ei.value.code == "source_unreachable"

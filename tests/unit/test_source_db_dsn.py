"""Unit tests for _attach_target_for and _resolve_scanner in source_db.

Covers SQLite prefix stripping and Postgres libpq key=value rebuilding.
No running database is required — these are pure string-transformation tests.
"""
from __future__ import annotations

import pytest

from decoy_engine.graph.ops.source_db import _attach_target_for, _resolve_scanner


class TestResolveScanner:
    def test_sqlite_returns_sqlite_scanner(self):
        assert _resolve_scanner("sqlite:///path/to/db") == ("sqlite_scanner", "sqlite")

    def test_postgresql_returns_postgres_scanner(self):
        assert _resolve_scanner("postgresql://user:pass@host/db") == ("postgres_scanner", "postgres")

    def test_postgresql_with_driver_prefix(self):
        # postgresql+psycopg2:// is a valid SQLAlchemy URL; the +driver suffix is stripped.
        assert _resolve_scanner("postgresql+psycopg2://user:pass@host/db") == ("postgres_scanner", "postgres")

    def test_mysql_returns_none(self):
        assert _resolve_scanner("mysql://user:pass@host/db") is None

    def test_mssql_returns_none(self):
        assert _resolve_scanner("mssql+pymssql://user:pass@host/db") is None


class TestAttachTargetForSqlite:
    def test_triple_slash_relative_path(self):
        result = _attach_target_for("sqlite:///relative/path.db", "sqlite")
        assert result == "relative/path.db"

    def test_four_slashes_absolute_path(self):
        # Four slashes = Unix absolute path; DuckDB wants the plain filesystem path.
        result = _attach_target_for("sqlite:////abs/path.db", "sqlite")
        assert result == "/abs/path.db"

    def test_double_slash_bare(self):
        result = _attach_target_for("sqlite://mydb.db", "sqlite")
        assert result == "mydb.db"

    def test_no_recognized_prefix_passes_through(self):
        result = _attach_target_for("/data/mydb.sqlite", "sqlite")
        assert result == "/data/mydb.sqlite"


class TestAttachTargetForPostgres:
    def test_full_dsn_all_components(self):
        dsn = "postgresql://admin:secret@dbhost:5432/mydb"
        result = _attach_target_for(dsn, "postgres")
        assert result == "dbname=mydb host=dbhost port=5432 user=admin password=secret"

    def test_minimal_dsn_host_and_db_only(self):
        dsn = "postgresql://dbhost/mydb"
        result = _attach_target_for(dsn, "postgres")
        assert result == "dbname=mydb host=dbhost"

    def test_no_password(self):
        dsn = "postgresql://admin@dbhost:5432/mydb"
        result = _attach_target_for(dsn, "postgres")
        assert result == "dbname=mydb host=dbhost port=5432 user=admin"

    def test_url_encoded_credentials_are_decoded(self):
        # percent-encoded @ signs in user/password must be decoded for libpq.
        dsn = "postgresql://admin%40corp:p%40ss@host/db"
        result = _attach_target_for(dsn, "postgres")
        assert result == "dbname=db host=host user=admin@corp password=p@ss"

    def test_psycopg2_driver_prefix(self):
        dsn = "postgresql+psycopg2://user:pass@host:5432/db"
        result = _attach_target_for(dsn, "postgres")
        assert result == "dbname=db host=host port=5432 user=user password=pass"

    def test_local_socket_dsn_no_host(self):
        # postgresql:///dbname connects via Unix socket; no host component.
        dsn = "postgresql:///localdb"
        result = _attach_target_for(dsn, "postgres")
        assert result == "dbname=localdb"

    def test_output_order_is_stable(self):
        # Parts always appear in: dbname host port user password order.
        dsn = "postgresql://u:p@h:9999/db"
        parts = _attach_target_for(dsn, "postgres").split()
        assert parts[0].startswith("dbname=")
        assert parts[1].startswith("host=")
        assert parts[2].startswith("port=")
        assert parts[3].startswith("user=")
        assert parts[4].startswith("password=")


class TestAttachTargetForFallback:
    def test_unknown_attach_type_returns_dsn_unchanged(self):
        dsn = "mysql://user:pass@host/db"
        assert _attach_target_for(dsn, "mysql") == dsn

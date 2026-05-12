"""Tests for the read-only SQL discovery helper.

Two surfaces under test: the SELECT-only statement filter, and the
end-to-end Parquet-backed query path. Parquet fixtures are written to
the per-test tmp_path so DuckDB's read_parquet can pick them up the
same way it would on the platform.
"""
from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.data_discovery import (
    DiscoverySqlError,
    run_discovery_sql,
)


def _write_parquet(tmp_path: Path, name: str, table: pa.Table) -> str:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


@pytest.fixture
def customers_table(tmp_path: Path) -> dict[str, str]:
    table = pa.table({
        "id": [1, 2, 3, 4],
        "country": ["US", "US", "UK", "FR"],
        "amount": [10.0, 20.0, 30.0, 40.0],
    })
    return {"customers": _write_parquet(tmp_path, "customers", table)}


class TestValidation:
    def test_empty_sql_rejected(self, customers_table):
        with pytest.raises(DiscoverySqlError, match="empty"):
            run_discovery_sql("", customers_table)

    def test_insert_rejected(self, customers_table):
        with pytest.raises(DiscoverySqlError, match="read-only"):
            run_discovery_sql("INSERT INTO customers VALUES (1)", customers_table)

    def test_drop_rejected(self, customers_table):
        with pytest.raises(DiscoverySqlError, match="read-only"):
            run_discovery_sql("DROP TABLE customers", customers_table)

    def test_pragma_rejected(self, customers_table):
        with pytest.raises(DiscoverySqlError, match="read-only"):
            run_discovery_sql("PRAGMA database_list", customers_table)

    def test_multi_statement_rejected(self, customers_table):
        # The second statement is what we really care about — make sure
        # the splitter catches it even though the first is a benign SELECT.
        with pytest.raises(DiscoverySqlError, match="Multiple statements"):
            run_discovery_sql(
                "SELECT 1; DROP TABLE customers", customers_table,
            )

    def test_sneaky_drop_in_cte_rejected(self, customers_table):
        # WITH is a permitted leader; the banned-keyword scan needs to
        # catch the DROP buried inside.
        with pytest.raises(DiscoverySqlError, match="DROP"):
            run_discovery_sql(
                "WITH x AS (DROP TABLE customers) SELECT 1", customers_table,
            )

    def test_with_select_allowed(self, customers_table):
        out = run_discovery_sql(
            "WITH us AS (SELECT * FROM customers WHERE country='US') "
            "SELECT count(*) AS n FROM us",
            customers_table,
        )
        assert out.rows == [{"n": 2}]

    def test_trailing_semicolon_allowed(self, customers_table):
        out = run_discovery_sql(
            "SELECT count(*) AS n FROM customers;",
            customers_table,
        )
        assert out.rows == [{"n": 4}]

    def test_line_comment_before_select_allowed(self, customers_table):
        out = run_discovery_sql(
            "-- daily report\nSELECT count(*) AS n FROM customers",
            customers_table,
        )
        assert out.rows == [{"n": 4}]


class TestExecution:
    def test_select_star(self, customers_table):
        out = run_discovery_sql("SELECT * FROM customers ORDER BY id", customers_table)
        assert out.columns == ["id", "country", "amount"]
        assert len(out.rows) == 4
        assert out.rows[0] == {"id": 1, "country": "US", "amount": 10.0}

    def test_group_by_aggregate(self, customers_table):
        out = run_discovery_sql(
            "SELECT country, sum(amount) AS total "
            "FROM customers GROUP BY country ORDER BY total DESC",
            customers_table,
        )
        assert out.columns == ["country", "total"]
        # FR=40, US=30, UK=30 — sort stable on equal totals so just check FR first.
        assert out.rows[0] == {"country": "FR", "total": 40.0}

    def test_row_limit_truncates(self, customers_table):
        out = run_discovery_sql(
            "SELECT * FROM customers", customers_table, row_limit=2,
        )
        assert len(out.rows) == 2

    def test_invalid_table_name_surfaces_duckdb_error(self, customers_table):
        with pytest.raises(DiscoverySqlError, match="SQL execution failed"):
            run_discovery_sql("SELECT * FROM nope", customers_table)

    def test_join_across_tables(self, tmp_path):
        a = pa.table({"id": [1, 2], "name": ["x", "y"]})
        b = pa.table({"id": [1, 2], "tag": ["a", "b"]})
        tables = {
            "a": _write_parquet(tmp_path, "a", a),
            "b": _write_parquet(tmp_path, "b", b),
        }
        out = run_discovery_sql(
            "SELECT a.name, b.tag FROM a JOIN b ON a.id = b.id ORDER BY a.id",
            tables,
        )
        assert out.rows == [
            {"name": "x", "tag": "a"},
            {"name": "y", "tag": "b"},
        ]

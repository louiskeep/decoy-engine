"""Phase 4 parity tests: DuckDB source / sink ops match the pandas legacy.

Calls each op's apply() with __engine='pandas' and __engine='duckdb'
explicitly (the runner does this in production via the registry; tests
exercise both code paths directly so we don't depend on the runner here).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.graph.ops import source_db, source_file, target_db, target_file


@pytest.fixture
def tmp_csv():
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "in.csv")
    pd.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "name": ["a", "b", "c", "d"],
            "value": [10, 20, 30, 40],
        }
    ).to_csv(src, index=False)
    return src, tmpdir


@pytest.fixture
def tmp_parquet():
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "in.parquet")
    pd.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "name": ["a", "b", "c", "d"],
            "value": [10.5, 20.5, 30.5, 40.5],
        }
    ).to_parquet(src, index=False)
    return src, tmpdir


def _to_pd(df):
    if isinstance(df, pa.Table):
        return df.to_pandas()
    return df


# -------- source.file ------------------------------------------------------


def test_source_file_csv_parity_pandas_vs_duckdb(tmp_csv):
    src, _ = tmp_csv
    pd_out = source_file.apply([], {"path": src, "__engine": "pandas"}, ctx=None)
    db_out = source_file.apply([], {"path": src, "__engine": "duckdb"}, ctx=None)
    pd.testing.assert_frame_equal(
        _to_pd(pd_out).reset_index(drop=True),
        _to_pd(db_out).reset_index(drop=True),
        check_dtype=False,
    )


def test_source_file_parquet_parity_pandas_vs_duckdb(tmp_parquet):
    src, _ = tmp_parquet
    pd_out = source_file.apply([], {"path": src, "__engine": "pandas"}, ctx=None)
    db_out = source_file.apply([], {"path": src, "__engine": "duckdb"}, ctx=None)
    pd.testing.assert_frame_equal(
        _to_pd(pd_out).reset_index(drop=True),
        _to_pd(db_out).reset_index(drop=True),
        check_dtype=False,
    )


def test_source_file_csv_preview_row_limit_honored_by_duckdb(tmp_csv):
    src, _ = tmp_csv
    out = source_file.apply(
        [],
        {"path": src, "__engine": "duckdb", "__preview_row_limit": 2},
        ctx=None,
    )
    assert _to_pd(out).shape[0] == 2


def test_source_file_duckdb_returns_arrow_table(tmp_csv):
    src, _ = tmp_csv
    out = source_file.apply([], {"path": src, "__engine": "duckdb"}, ctx=None)
    assert isinstance(out, pa.Table), (
        f"duckdb-mode source.file must return pyarrow.Table; got {type(out)}"
    )


# -------- target.file ------------------------------------------------------


def test_target_file_csv_parity_pandas_vs_duckdb(tmp_csv):
    src, tmpdir = tmp_csv
    df = pd.read_csv(src)
    table = pa.Table.from_pandas(df, preserve_index=False)
    pd_path = os.path.join(tmpdir, "out_pandas.csv")
    db_path = os.path.join(tmpdir, "out_duckdb.csv")

    target_file.apply([df], {"output_filename": pd_path, "__engine": "pandas"}, ctx=None)
    target_file.apply([table], {"output_filename": db_path, "__engine": "duckdb"}, ctx=None)

    written_pd = pd.read_csv(pd_path)
    written_db = pd.read_csv(db_path)
    pd.testing.assert_frame_equal(
        written_pd.reset_index(drop=True),
        written_db.reset_index(drop=True),
        check_dtype=False,
    )


def test_target_file_parquet_parity_pandas_vs_duckdb(tmp_parquet):
    src, tmpdir = tmp_parquet
    df = pd.read_parquet(src)
    table = pa.Table.from_pandas(df, preserve_index=False)
    pd_path = os.path.join(tmpdir, "out_pandas.parquet")
    db_path = os.path.join(tmpdir, "out_duckdb.parquet")

    target_file.apply([df], {"output_filename": pd_path, "__engine": "pandas"}, ctx=None)
    target_file.apply([table], {"output_filename": db_path, "__engine": "duckdb"}, ctx=None)

    written_pd = pd.read_parquet(pd_path)
    written_db = pd.read_parquet(db_path)
    pd.testing.assert_frame_equal(
        written_pd.reset_index(drop=True),
        written_db.reset_index(drop=True),
        check_dtype=False,
    )


def test_target_file_preview_skips_write_in_duckdb_mode(tmp_csv):
    src, tmpdir = tmp_csv
    df = pd.read_csv(src)
    table = pa.Table.from_pandas(df, preserve_index=False)
    out_path = os.path.join(tmpdir, "preview.csv")

    target_file.apply(
        [table],
        {"output_filename": out_path, "__engine": "duckdb", "__preview_row_limit": 50},
        ctx=None,
    )
    assert not os.path.exists(out_path), "preview must not write the file"


# -------- source.db / target.db (SQLite via SQLAlchemy DSN) ----------------


@pytest.fixture
def tmp_sqlite():
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, value REAL)")
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?, ?)",
        [(1, "Alice", 10.5), (2, "Bob", 20.5), (3, "Carol", 30.5)],
    )
    conn.commit()
    conn.close()
    return db_path, tmpdir


def test_source_db_parity_pandas_vs_duckdb(tmp_sqlite):
    db_path, _ = tmp_sqlite
    dsn = f"sqlite:///{db_path}"
    pd_out = source_db.apply(
        [],
        {"table": "customers", "dsn": dsn, "__engine": "pandas"},
        ctx=None,
    )
    db_out = source_db.apply(
        [],
        {"table": "customers", "dsn": dsn, "__engine": "duckdb"},
        ctx=None,
    )
    pd.testing.assert_frame_equal(
        _to_pd(pd_out).reset_index(drop=True),
        _to_pd(db_out).reset_index(drop=True),
        check_dtype=False,
    )


def test_target_db_parity_pandas_vs_duckdb(tmp_sqlite):
    db_path, _tmpdir = tmp_sqlite
    dsn = f"sqlite:///{db_path}"
    df = pd.DataFrame({"id": [10, 11], "name": ["x", "y"], "value": [1.0, 2.0]})

    # Two destinations to keep the writes isolated
    target_db.apply(
        [df],
        {"table": "appended_pandas", "dsn": dsn, "__engine": "pandas", "write_mode": "replace"},
        ctx=None,
    )
    target_db.apply(
        [pa.Table.from_pandas(df, preserve_index=False)],
        {"table": "appended_duckdb", "dsn": dsn, "__engine": "duckdb", "write_mode": "replace"},
        ctx=None,
    )

    conn = sqlite3.connect(db_path)
    pd_rows = pd.read_sql_query("SELECT * FROM appended_pandas ORDER BY id", conn)
    db_rows = pd.read_sql_query("SELECT * FROM appended_duckdb ORDER BY id", conn)
    conn.close()
    pd.testing.assert_frame_equal(
        pd_rows.reset_index(drop=True),
        db_rows.reset_index(drop=True),
        check_dtype=False,
    )


# -------- target.db write-mode coverage on the SQLite scanner path --------
# These tests are not "parity" with pandas — they verify the new DuckDB
# native scanner correctly implements the write_mode semantics. Bug 3.


def test_target_db_replace_overwrites_existing_rows(tmp_sqlite):
    db_path, _ = tmp_sqlite
    dsn = f"sqlite:///{db_path}"

    seed = pa.Table.from_pylist(
        [
            {"id": 1, "name": "first", "value": 1.0},
            {"id": 2, "name": "second", "value": 2.0},
        ]
    )
    target_db.apply(
        [seed],
        {"table": "rmode", "dsn": dsn, "__engine": "duckdb", "write_mode": "replace"},
        ctx=None,
    )
    # Replace with a different shape — should fully replace, not merge.
    fresh = pa.Table.from_pylist(
        [
            {"id": 99, "name": "only-row", "value": 9.0},
        ]
    )
    target_db.apply(
        [fresh],
        {"table": "rmode", "dsn": dsn, "__engine": "duckdb", "write_mode": "replace"},
        ctx=None,
    )

    conn = sqlite3.connect(db_path)
    rows = pd.read_sql_query("SELECT * FROM rmode ORDER BY id", conn)
    conn.close()
    assert list(rows["id"]) == [99]
    assert list(rows["name"]) == ["only-row"]


def test_target_db_append_extends_existing_table(tmp_sqlite):
    db_path, _ = tmp_sqlite
    dsn = f"sqlite:///{db_path}"

    seed = pa.Table.from_pylist(
        [
            {"id": 1, "name": "first", "value": 1.0},
        ]
    )
    target_db.apply(
        [seed],
        {"table": "amode", "dsn": dsn, "__engine": "duckdb", "write_mode": "replace"},
        ctx=None,
    )
    extra = pa.Table.from_pylist(
        [
            {"id": 2, "name": "second", "value": 2.0},
            {"id": 3, "name": "third", "value": 3.0},
        ]
    )
    target_db.apply(
        [extra],
        {"table": "amode", "dsn": dsn, "__engine": "duckdb", "write_mode": "append"},
        ctx=None,
    )

    conn = sqlite3.connect(db_path)
    rows = pd.read_sql_query("SELECT * FROM amode ORDER BY id", conn)
    conn.close()
    assert list(rows["id"]) == [1, 2, 3]


def test_target_db_fail_mode_raises_when_table_exists(tmp_sqlite):
    """`write_mode: fail` is the safety hatch — if the destination
    table already exists, the op refuses to write rather than silently
    appending or replacing."""
    db_path, _ = tmp_sqlite
    dsn = f"sqlite:///{db_path}"

    seed = pa.Table.from_pylist([{"id": 1, "name": "x", "value": 1.0}])
    target_db.apply(
        [seed],
        {"table": "fmode", "dsn": dsn, "__engine": "duckdb", "write_mode": "replace"},
        ctx=None,
    )

    from decoy_engine.graph.ops._base import OpError

    with pytest.raises(OpError):
        target_db.apply(
            [seed],
            {"table": "fmode", "dsn": dsn, "__engine": "duckdb", "write_mode": "fail"},
            ctx=None,
        )


def test_source_db_with_where_clause_filters_via_duckdb_scanner(tmp_sqlite):
    """The native scanner path should honor the optional `where` config
    just like the SQLAlchemy fallback. Validates the SQL the dispatcher
    builds gets routed correctly into DuckDB's executor."""
    db_path, _ = tmp_sqlite
    dsn = f"sqlite:///{db_path}"

    out = source_db.apply(
        [],
        {
            "table": "customers",
            "dsn": dsn,
            "__engine": "duckdb",
            "where": "value > 15",
        },
        ctx=None,
    )
    df = _to_pd(out).reset_index(drop=True)
    assert list(df["id"]) == [2, 3]  # Bob (20.5), Carol (30.5); Alice (10.5) filtered out


def test_source_db_preview_row_limit_honored_by_duckdb_scanner(tmp_sqlite):
    """Preview mode passes `__preview_row_limit` through; the scanner
    SQL should LIMIT accordingly so we don't drag the whole source
    when a node is just being previewed in the UI."""
    db_path, _ = tmp_sqlite
    dsn = f"sqlite:///{db_path}"

    out = source_db.apply(
        [],
        {
            "table": "customers",
            "dsn": dsn,
            "__engine": "duckdb",
            "__preview_row_limit": 2,
        },
        ctx=None,
    )
    assert len(_to_pd(out)) == 2

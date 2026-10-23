"""DuckDB-file source adapter (DuckDB sources D2).

Fully exercised in-sandbox: a .duckdb file needs no scanner extension, so the
whole attach -> validate -> ordered COPY -> atomic Parquet publish path runs
here against a real file, plus the catalog browse over it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pq = pytest.importorskip("pyarrow.parquet")

from decoy_engine.connectors.duckdb_attach import (
    DuckDbFileDialect,
    DuckDbFileError,
    RelationSelection,
    SnapshotConfig,
    browse,
    run_snapshot,
)


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


def _make_db(path: Path) -> None:
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE orders(id INTEGER, amt DOUBLE, note VARCHAR)")
    con.execute("INSERT INTO orders VALUES (2, 1.25, 'b'), (1, 9.5, 'a'), (3, 4.0, 'c')")
    con.execute("CREATE TABLE customers(cid INTEGER, name VARCHAR)")
    con.close()


def _sel(**kw):
    base = {"schema": "main", "table": "orders", "columns": ["id", "amt", "note"]}
    base.update(kw)
    return RelationSelection(**base)


def test_snapshot_round_trips_ordered(tmp_path: Path) -> None:
    src = tmp_path / "src.duckdb"
    _make_db(src)
    out = run_snapshot(
        _config(src, tmp_path), [_sel(order_by=["id"])], tmp_path / "dest", dialect=DuckDbFileDialect()
    )
    path = out["main.orders"]
    assert path.exists()
    tbl = pq.read_table(path).to_pydict()
    assert tbl["id"] == [1, 2, 3]  # ordered by the key, deterministic
    assert tbl["note"] == ["a", "b", "c"]


def test_column_projection_and_order(tmp_path: Path) -> None:
    src = tmp_path / "src.duckdb"
    _make_db(src)
    out = run_snapshot(
        _config(src, tmp_path),
        [_sel(columns=["id", "note"], order_by=["id"])],
        tmp_path / "dest",
        dialect=DuckDbFileDialect(),
    )
    cols = pq.read_table(out["main.orders"]).column_names
    assert cols == ["id", "note"]  # only the projected columns, in order


def test_relation_not_found_is_coded(tmp_path: Path) -> None:
    src = tmp_path / "src.duckdb"
    _make_db(src)
    with pytest.raises(DuckDbFileError) as ei:
        run_snapshot(
            _config(src, tmp_path),
            [_sel(table="ghost")],
            tmp_path / "dest",
            dialect=DuckDbFileDialect(),
        )
    assert ei.value.code == "relation_not_found"


def test_column_not_found_is_coded_and_publishes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "src.duckdb"
    _make_db(src)
    dest = tmp_path / "dest"
    with pytest.raises(DuckDbFileError) as ei:
        run_snapshot(
            _config(src, tmp_path),
            [_sel(columns=["id", "nope"])],
            dest,
            dialect=DuckDbFileDialect(),
        )
    assert ei.value.code == "column_not_found"
    # Nothing published on failure.
    assert not any(dest.glob("snapshot-*")) if dest.exists() else True


def test_multi_relation_one_snapshot(tmp_path: Path) -> None:
    src = tmp_path / "src.duckdb"
    _make_db(src)
    out = run_snapshot(
        _config(src, tmp_path),
        [
            _sel(order_by=["id"]),
            RelationSelection(schema="main", table="customers", columns=["cid", "name"]),
        ],
        tmp_path / "dest",
        dialect=DuckDbFileDialect(),
    )
    assert set(out) == {"main.orders", "main.customers"}
    # Both land under one published snapshot dir.
    assert {p.parent for p in out.values()} == {next(iter(out.values())).parent}


def test_browse_over_duckdb_file(tmp_path: Path) -> None:
    src = tmp_path / "src.duckdb"
    _make_db(src)
    listing = browse(_config(src, tmp_path), dialect=DuckDbFileDialect())
    assert set(listing.schemas) == {"main"}
    orders = next(t for t in listing.tables if t.table == "orders")
    assert [c.name for c in orders.columns] == ["id", "amt", "note"]
    assert all(c.verdict == "supported" and c.note == "native" for c in orders.columns)

"""Path-based lazy source abstraction and fixture Parquet writers.

`LazySource` wraps a Parquet file path so a source table can be consumed in
bounded batches instead of held fully resident; this is the enabling piece for
a later out-of-core runner rewrite (not wired to the runner here). These tests
pin its read contract (batch bounds, schema/row-count from metadata, whole-file
fallback) plus the two fixture writers that put on-disk sources in front of it:
one that mirrors `build_table` byte-for-byte, and one that generates a large FK
chain straight to Parquet without ever holding a whole table resident.
"""

from __future__ import annotations

import math

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution.out_of_core import LazySource
from decoy_engine.execution.out_of_core._source import LazySource as LazySourcePrivate
from tests.perf_fixtures.fk_relational import (
    build_table,
    lazy_sources,
    write_large_fk_chain,
    write_table_parquet,
)


def _write(table: pa.Table, path) -> None:
    pq.write_table(table, path)


def test_lazy_source_is_public_export():
    assert LazySource is LazySourcePrivate


def test_iter_batches_concatenates_to_original_table(tmp_path):
    table = pa.table({"a": list(range(37)), "b": [str(i) for i in range(37)]})
    path = tmp_path / "t.parquet"
    _write(table, path)

    source = LazySource(path=path)
    batches = list(source.iter_batches(batch_rows=10))
    rebuilt = pa.Table.from_batches(batches)

    assert rebuilt.equals(table)


def test_iter_batches_bounds_each_batch(tmp_path):
    table = pa.table({"a": list(range(25))})
    path = tmp_path / "t.parquet"
    _write(table, path)

    source = LazySource(path=path)
    batches = list(source.iter_batches(batch_rows=7))

    assert len(batches) > 1
    assert all(b.num_rows <= 7 for b in batches)
    assert sum(b.num_rows for b in batches) == 25


def test_schema_and_num_rows_match_metadata_without_reading_data(tmp_path):
    table = pa.table({"a": pa.array([1, 2, 3], type=pa.int64()), "b": ["x", "y", "z"]})
    path = tmp_path / "t.parquet"
    _write(table, path)

    source = LazySource(path=path)
    metadata = pq.read_metadata(path)

    assert source.num_rows == metadata.num_rows == 3
    assert source.schema == metadata.schema.to_arrow_schema()


def test_to_table_reads_whole_file(tmp_path):
    table = pa.table({"a": [1, 2, 3]})
    path = tmp_path / "t.parquet"
    _write(table, path)

    source = LazySource(path=path)

    assert source.to_table().equals(table)


def test_write_table_parquet_round_trips_byte_identical(tmp_path):
    path = tmp_path / "child.parquet"
    out_path = write_table_parquet("child", rows=200, path=path, width=3, orphan_frac=0.1, seed=42)

    expected = build_table("child", rows=200, width=3, orphan_frac=0.1, seed=42)
    actual = pq.read_table(out_path)

    assert actual.equals(expected)


def test_write_table_parquet_defaults_match_build_table_defaults(tmp_path):
    path = tmp_path / "parent.parquet"
    out_path = write_table_parquet("parent", rows=50, path=path)

    expected = build_table("parent", rows=50)
    actual = pq.read_table(out_path)

    assert actual.equals(expected)


def test_write_large_fk_chain_writes_three_files_with_correct_row_counts(tmp_path):
    rows = 500
    paths = write_large_fk_chain(tmp_path / "chain", rows=rows, width=2, batch_rows=100)

    assert set(paths) == {"parent", "child", "grandchild"}
    for name, path in paths.items():
        assert path.exists(), name
        assert pq.read_metadata(path).num_rows == rows, name


def test_write_large_fk_chain_writes_more_than_one_row_group(tmp_path):
    rows = 500
    paths = write_large_fk_chain(tmp_path / "chain", rows=rows, width=1, batch_rows=100)

    for name, path in paths.items():
        n_row_groups = pq.ParquetFile(path).metadata.num_row_groups
        assert n_row_groups == math.ceil(rows / 100), name
        assert n_row_groups > 1, name


def test_write_large_fk_chain_fk_integrity_holds_with_no_orphans(tmp_path):
    rows = 300
    paths = write_large_fk_chain(tmp_path / "chain", rows=rows, width=1, batch_rows=64)

    parent_ids = set(pq.read_table(paths["parent"]).column("id").to_pylist())
    child_table = pq.read_table(paths["child"])
    child_ids = set(child_table.column("id").to_pylist())
    grandchild_table = pq.read_table(paths["grandchild"])

    assert len(parent_ids) == rows
    assert len(child_ids) == rows
    for parent_id in child_table.column("parent_id").to_pylist():
        assert parent_id in parent_ids
    for child_id in grandchild_table.column("child_id").to_pylist():
        assert child_id in child_ids


def test_write_large_fk_chain_plants_approximate_orphan_fraction(tmp_path):
    rows = 1_000
    orphan_frac = 0.1
    paths = write_large_fk_chain(
        tmp_path / "chain", rows=rows, width=1, orphan_frac=orphan_frac, batch_rows=128
    )

    parent_ids = set(pq.read_table(paths["parent"]).column("id").to_pylist())
    child_ids = set(pq.read_table(paths["child"]).column("id").to_pylist())
    child_table = pq.read_table(paths["child"])
    grandchild_table = pq.read_table(paths["grandchild"])

    child_orphans = sum(
        1 for v in child_table.column("parent_id").to_pylist() if v not in parent_ids
    )
    grandchild_orphans = sum(
        1 for v in grandchild_table.column("child_id").to_pylist() if v not in child_ids
    )

    assert math.isclose(child_orphans / rows, orphan_frac, abs_tol=0.02)
    assert math.isclose(grandchild_orphans / rows, orphan_frac, abs_tol=0.02)


def test_write_large_fk_chain_zero_orphan_frac_is_exact(tmp_path):
    rows = 200
    paths = write_large_fk_chain(tmp_path / "chain", rows=rows, width=1, orphan_frac=0.0)

    parent_ids = set(pq.read_table(paths["parent"]).column("id").to_pylist())
    child_table = pq.read_table(paths["child"])
    for parent_id in child_table.column("parent_id").to_pylist():
        assert parent_id in parent_ids


def test_lazy_sources_wraps_paths_into_working_lazysources(tmp_path):
    rows = 40
    parent_path = write_table_parquet("parent", rows=rows, path=tmp_path / "parent.parquet")
    child_path = write_table_parquet("child", rows=rows, path=tmp_path / "child.parquet")

    sources = lazy_sources({"parent": parent_path, "child": child_path})

    assert set(sources) == {"parent", "child"}
    for source in sources.values():
        assert isinstance(source, LazySource)
    assert sources["parent"].num_rows == rows
    assert sources["parent"].to_table().equals(build_table("parent", rows=rows))


def test_write_large_fk_chain_schema_matches_build_table_shape(tmp_path):
    rows = 30
    width = 2
    paths = write_large_fk_chain(tmp_path / "chain", rows=rows, width=width)

    parent_schema = pq.read_metadata(paths["parent"]).schema.to_arrow_schema()
    expected_parent_cols = {"id"} | {f"payload_{i:02d}" for i in range(width)}
    assert set(parent_schema.names) == expected_parent_cols

    child_schema = pq.read_metadata(paths["child"]).schema.to_arrow_schema()
    expected_child_cols = {"id", "parent_id"} | {f"payload_{i:02d}" for i in range(width)}
    assert set(child_schema.names) == expected_child_cols

    grandchild_schema = pq.read_metadata(paths["grandchild"]).schema.to_arrow_schema()
    expected_grandchild_cols = {"child_id"} | {f"payload_{i:02d}" for i in range(width)}
    assert set(grandchild_schema.names) == expected_grandchild_cols

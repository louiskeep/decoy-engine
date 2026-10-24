"""`decoy_engine.io.bounded_reader` (parity E2a): chunks stay within the budget,
own their buffers, preserve every row and the CSV null contract, and fail closed
on an oversized row."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.io import (
    PANDAS_DEFAULT_NA_VALUES,
    ChunkBudget,
    CsvMalformed,
    CsvRecordExceedsMaxRowBytes,
    RowExceedsMaxRowBytes,
    iter_csv_chunks,
    iter_parquet_chunks,
    own_array,
)


def _budget(**kw) -> ChunkBudget:
    base = dict(
        chunk_budget_bytes=4096,
        max_chunk_rows=1000,
        max_row_bytes=1024,
        out_row_bound=0,
        read_rows=500,
    )
    base.update(kw)
    return ChunkBudget(**base)


def test_budget_validates_its_fields() -> None:
    with pytest.raises(ValueError):
        ChunkBudget(chunk_budget_bytes=0, max_chunk_rows=1, max_row_bytes=1)
    with pytest.raises(ValueError):
        ChunkBudget(chunk_budget_bytes=1, max_chunk_rows=1, max_row_bytes=1, out_row_bound=-1)


def test_parquet_chunks_cover_every_row_within_budget(tmp_path: Path) -> None:
    table = pa.table({"id": list(range(5000)), "txt": ["x" * 40] * 5000})
    path = tmp_path / "t.parquet"
    pq.write_table(table, path, row_group_size=1200)
    chunks = list(iter_parquet_chunks(str(path), _budget(), table="t"))
    assert len(chunks) > 1
    assert sum(c.num_rows for c in chunks) == 5000
    assert pa.concat_tables(chunks).equals(table)
    for c in chunks:
        assert c.num_rows <= 1000
        assert c.nbytes <= 4096 or c.num_rows == 1


def test_dictionary_columns_are_decoded_and_owned(tmp_path: Path) -> None:
    table = pa.table({"k": pa.array(["a", "b"] * 500).dictionary_encode()})
    path = tmp_path / "d.parquet"
    pq.write_table(table, path)
    chunks = list(iter_parquet_chunks(str(path), _budget(), table="d"))
    assert all(not pa.types.is_dictionary(c.schema.field("k").type) for c in chunks)
    assert pa.concat_tables(chunks).column("k").to_pylist() == ["a", "b"] * 500
    assert own_array(pa.array([1, 2]).slice(1, 1)).to_pylist() == [2]


def test_oversized_parquet_row_fails_closed_after_the_rows_before_it(tmp_path: Path) -> None:
    table = pa.table({"txt": ["ok"] * 10 + ["y" * 5000] + ["ok"]})
    path = tmp_path / "o.parquet"
    pq.write_table(table, path)
    seen = 0
    with pytest.raises(RowExceedsMaxRowBytes) as exc:
        for c in iter_parquet_chunks(str(path), _budget(), table="o"):
            seen += c.num_rows
    assert seen == 10 and exc.value.detail["row"] == 10


def test_csv_chunks_keep_the_pandas_null_contract_and_string_types(tmp_path: Path) -> None:
    df = pd.DataFrame({"a": ["1", "NA", "x", ""], "b": ["NULL", "y", "n/a", "z"]})
    path = tmp_path / "c.csv"
    df.to_csv(path, index=False)
    chunks = list(iter_csv_chunks(str(path), _budget(max_chunk_rows=2), table="c"))
    assert [c.num_rows for c in chunks] == [2, 2]
    out = pa.concat_tables(chunks)
    assert all(pa.types.is_string(f.type) for f in out.schema)
    full = pa.Table.from_pandas(pd.read_csv(path, dtype=str), preserve_index=False)
    assert out.column("a").to_pylist() == full.column("a").to_pylist()
    assert out.column("b").to_pylist() == full.column("b").to_pylist()
    assert "NA" in PANDAS_DEFAULT_NA_VALUES


def test_csv_record_over_the_cap_and_malformed_header_fail_closed(tmp_path: Path) -> None:
    big = tmp_path / "big.csv"
    big.write_bytes(b"a,b\n1," + b"x" * 5000 + b"\n")
    with pytest.raises(CsvRecordExceedsMaxRowBytes):
        list(iter_csv_chunks(str(big), _budget(), table="big"))
    bad = tmp_path / "bad.csv"
    bad.write_bytes(b'"open,b\n1,2\n')
    with pytest.raises(CsvMalformed):
        list(iter_csv_chunks(str(bad), _budget(), table="bad"))

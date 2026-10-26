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
    BoundedReadError,
    ChunkBudget,
    CsvMalformed,
    CsvRecordExceedsMaxRowBytes,
    RowExceedsMaxRowBytes,
    cut_batch,
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


def test_a_nested_column_is_rejected_before_the_bound_can_be_exceeded(tmp_path: Path) -> None:
    # Codex HIGH (parity E2a): a nested (list/struct) column was priced at a flat
    # 9 bytes regardless of its payload, so a list holding a 10,000-byte string
    # passed max_row_bytes=100 and the reader yielded a 10,008-byte row -- the
    # bounded-memory contract of this public reader silently broken. A nested
    # column cannot be priced per row from its type alone, so it is rejected with
    # a coded error BEFORE any chunk is yielded.
    big = "z" * 10_000
    table = pa.table({"vals": pa.array([["ok"], [big], ["ok"]], pa.list_(pa.string()))})
    path = tmp_path / "nested.parquet"
    pq.write_table(table, path)
    budget = _budget(chunk_budget_bytes=100, max_row_bytes=100)
    seen: list[pa.Table] = []
    with pytest.raises(BoundedReadError) as exc:
        for c in iter_parquet_chunks(str(path), budget, table="n"):
            seen.append(c)
    assert seen == []  # rejected before any chunk (and its oversized row) was yielded
    assert exc.value.code == "unsupported_column_type"
    assert "list" in str(exc.value.detail.get("arrow_type", ""))


def test_cut_batch_counts_eight_byte_offsets_for_large_string() -> None:
    # Codex HIGH (parity E2a): large_string/large_binary use 8-byte offsets, but the
    # per-row accounting charged only the 4-byte-offset overhead, so 40 empty
    # large_string values produced a single 40-row chunk whose offset buffer alone is
    # 320+ bytes under a 100-byte chunk budget. `cut_batch` is public (`decoy_engine.io`)
    # and `own_array` keeps the large_string type, so the chunk really does hold 8-byte
    # offsets. The accounting now counts them.
    batch = pa.RecordBatch.from_arrays([pa.array([""] * 40, pa.large_string())], names=["s"])
    budget = _budget(chunk_budget_bytes=100, max_chunk_rows=1000, max_row_bytes=1024)
    chunks = list(cut_batch(batch, budget, table="ls"))
    assert sum(c.num_rows for c in chunks) == 40
    for c in chunks:
        assert pa.types.is_large_string(c.schema.field("s").type)  # own_array kept the type
        # The offset buffer alone is 8 bytes per row; counting it, no multi-row chunk
        # may exceed the byte budget.
        assert c.num_rows * 8 <= budget.chunk_budget_bytes or c.num_rows == 1


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

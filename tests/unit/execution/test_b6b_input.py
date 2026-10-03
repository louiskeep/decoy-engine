"""Acceptance test 4 of plan 2026-10-02-b6b-lazy-batch-input (rev 2.1) plus the units under it:
the batch input is bounded structurally, re-cut to the resident chunk boundaries, and read
with the frozen reader options.

These tests are written before the implementation. Do not delete one, add a skip or xfail,
or raise a structural bound without a new plan gate.
"""

from __future__ import annotations

import gc
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.profile._readers import LazySource
from tests.unit.execution import _b6b_support as b6b

MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# rechunk
# ---------------------------------------------------------------------------


def _batches(sizes: list[int], start: int = 0) -> tuple[pa.Schema, list[pa.RecordBatch]]:
    schema = pa.schema([("v", pa.int64())])
    out: list[pa.RecordBatch] = []
    cursor = start
    for size in sizes:
        out.append(
            pa.RecordBatch.from_arrays(
                [pa.array(range(cursor, cursor + size), pa.int64())], schema=schema
            )
        )
        cursor += size
    return schema, out


def test_the_recorded_three_fragment_sequence_makes_one_exact_chunk() -> None:
    mod = b6b.chunked_input()
    schema, batches = _batches([16_666, 33_333, 1])
    chunks = list(mod.rechunk(iter(batches), schema, 50_000))
    assert [c.num_rows for c in chunks] == [50_000]
    assert chunks[0].column("v").to_pylist() == list(range(50_000))
    assert chunks[0].schema.equals(schema, check_metadata=True)


@pytest.mark.parametrize(
    ("sizes", "chunk"),
    [
        ([7, 7, 6], 7),
        ([3] * 10, 7),
        ([5, 5, 5, 5, 5], 12),
        ([1] * 25, 10),
        ([10, 10, 10], 10),
        ([4, 8, 1, 8, 2], 8),
        ([2, 2], 100),
    ],
)
def test_rechunk_cuts_exact_chunks_and_retains_fewer_than_two_chunks(
    sizes: list[int], chunk: int
) -> None:
    mod = b6b.chunked_input()
    schema, batches = _batches(sizes)
    assert all(s <= chunk for s in sizes), "reader batches never exceed the chunk size"
    consumed = {"rows": 0}

    def feed() -> Iterator[pa.RecordBatch]:
        for batch in batches:
            consumed["rows"] += batch.num_rows
            yield batch

    emitted = 0
    chunks: list[pa.Table] = []
    for table in mod.rechunk(feed(), schema, chunk):
        pending_before = consumed["rows"] - emitted
        assert pending_before < 2 * chunk
        emitted += table.num_rows
        chunks.append(table)
    total = sum(sizes)
    assert [c.num_rows for c in chunks[:-1]] == [chunk] * (len(chunks) - 1)
    assert 0 < chunks[-1].num_rows <= chunk
    assert emitted == total
    flat = [v for c in chunks for v in c.column("v").to_pylist()]
    assert flat == list(range(total))


def test_rechunk_of_nothing_yields_nothing() -> None:
    mod = b6b.chunked_input()
    schema, _ = _batches([])
    assert list(mod.rechunk(iter([]), schema, 10)) == []


def test_rechunk_drops_empty_batches() -> None:
    mod = b6b.chunked_input()
    schema, batches = _batches([0, 4, 0, 4, 0])
    chunks = list(mod.rechunk(iter(batches), schema, 4))
    assert [c.num_rows for c in chunks] == [4, 4]


# ---------------------------------------------------------------------------
# OpenedLazyBatches and the reader options
# ---------------------------------------------------------------------------


def _small(tmp_path: Path, rows: int = 100, group: int = 30) -> LazySource:
    table = pa.table(
        {"v": pa.array(range(rows), pa.int64()), "s": pa.array([f"x{i}" for i in range(rows)])}
    )
    return b6b.lazy(b6b.write(table, tmp_path / "small.parquet", row_group_size=group))


def test_open_batches_returns_an_owner_with_footer_facts_and_an_idempotent_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _small(tmp_path)
    spy = b6b.ParquetFileSpy(monkeypatch)
    opened = src.open_batches(40)
    assert opened.schema.equals(pq.read_schema(src.path), check_metadata=True)
    assert opened.num_rows == 100
    assert (opened.row_groups, opened.max_row_group_rows) == (4, 30)
    assert sum(b.num_rows for b in opened.batches) == 100
    assert len(spy.opened) == 1 and spy.closed == []
    opened.close()
    opened.close()
    assert len(spy.closed) == 1


def test_open_batches_without_pre_buffer_passes_no_pre_buffer_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _small(tmp_path)
    spy = b6b.ParquetFileSpy(monkeypatch)
    opened = src.open_batches(40)
    list(opened.batches)
    opened.close()
    assert "pre_buffer" not in spy.opened[0], spy.opened[0]
    assert "buffer_size" not in spy.opened[0] or spy.opened[0]["buffer_size"] == 0
    explicit = src.open_batches(40, pre_buffer=True, buffer_size=MIB, use_threads=False)
    list(explicit.batches)
    explicit.close()
    assert spy.opened[1]["pre_buffer"] is True
    assert spy.opened[1]["buffer_size"] == MIB
    assert spy.iter_calls[1]["use_threads"] is False


def test_the_lane_reads_with_the_frozen_reader_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6b.chunked_input()
    assert mod.INPUT_BUFFER_BYTES == 8 * MIB
    cfg, path = b6b.stream_job(tmp_path, 3_000, 1_000)
    for threads in (1, 4):
        spy = b6b.ParquetFileSpy(monkeypatch)
        b6b.run(cfg, {b6b.TABLE: b6b.lazy(path)}, b6b.CountingSink(), native_threads=threads)
        reading = [o for o in spy.opened if o.get("pre_buffer") is False]
        assert len(reading) == 1, spy.opened
        assert reading[0]["buffer_size"] == mod.INPUT_BUFFER_BYTES
        assert [c["use_threads"] for c in spy.iter_calls if "use_threads" in c] == [threads > 1]


# ---------------------------------------------------------------------------
# open_input
# ---------------------------------------------------------------------------


def _facts(src: LazySource) -> Any:
    mod = b6b.chunked_input()
    return mod.capture_source_facts({b6b.TABLE: src}, {b6b.TABLE: "mask"})[b6b.TABLE]


def test_open_input_of_a_table_yields_the_resident_slices(tmp_path: Path) -> None:
    mod = b6b.chunked_input()
    table = pa.table({"v": pa.array(range(25), pa.int64())})
    inputs = mod.open_input(
        table, table=b6b.TABLE, chunk_size_rows=10, expected=None, native_threads=1
    )
    assert inputs.first.equals(table.slice(0, 10))
    assert [c.num_rows for c in inputs.chunks] == [10, 10, 5]
    inputs.close()
    inputs.close()
    assert inputs.block() == {"mode": "resident", "reason": "resident_source"}


def test_open_input_of_a_lazy_source_yields_chunks_equal_to_the_resident_slices(
    tmp_path: Path,
) -> None:
    mod = b6b.chunked_input()
    src = _small(tmp_path, 100, 30)
    resident = pq.read_table(src.path)
    inputs = mod.open_input(
        src, table=b6b.TABLE, chunk_size_rows=16, expected=_facts(src), native_threads=1
    )
    chunks = list(inputs.chunks)
    assert inputs.first.equals(resident.slice(0, 16))
    assert [c.num_rows for c in chunks] == [16] * 6 + [4]
    for index, chunk in enumerate(chunks):
        assert chunk.equals(resident.slice(index * 16, 16), check_metadata=True), index
    assert inputs.block() == {
        "mode": "lazy",
        "reason": "eligible",
        "source_row_groups": 4,
        "source_max_row_group_rows": 30,
    }


def test_open_input_closes_its_owner_on_exhaustion_and_on_an_early_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6b.chunked_input()
    src = _small(tmp_path)
    spy = b6b.ParquetFileSpy(monkeypatch)
    inputs = mod.open_input(
        src, table=b6b.TABLE, chunk_size_rows=16, expected=_facts(src), native_threads=1
    )
    reader = [pf for pf in spy.instances][-1]
    list(inputs.chunks)
    assert reader in spy.closed
    again = mod.open_input(
        src, table=b6b.TABLE, chunk_size_rows=16, expected=_facts(src), native_threads=1
    )
    reader2 = spy.instances[-1]
    again.close()
    again.close()
    assert spy.closed.count(reader2) == 1


def test_open_input_with_a_changed_row_count_raises_before_any_chunk_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6b.chunked_input()
    src = _small(tmp_path, 100, 30)
    facts = _facts(src)
    b6b.write(
        pa.table({"v": pa.array(range(90), pa.int64()), "s": pa.array(["x"] * 90)}), src.path, 30
    )
    spy = b6b.ParquetFileSpy(monkeypatch)
    err = b6b.fails_with(
        ExecutionError,
        lambda: mod.open_input(
            src, table=b6b.TABLE, chunk_size_rows=16, expected=facts, native_threads=1
        ),
    )
    assert getattr(err, "code", None) == "lazy_source_changed"
    assert spy.instances and all(pf in spy.closed for pf in spy.instances)


def test_open_input_flags_a_short_read_at_the_end_of_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6b.chunked_input()
    src = _small(tmp_path, 100, 30)
    facts = _facts(src)
    real_iter = pq.ParquetFile.iter_batches

    def short(self: Any, *args: Any, **kwargs: Any) -> Iterator[pa.RecordBatch]:
        cut = False
        for batch in real_iter(self, *args, **kwargs):
            if not cut and batch.num_rows > 1:
                cut = True
                yield batch.slice(0, batch.num_rows - 1)
            else:
                yield batch

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", short)
    inputs = mod.open_input(
        src, table=b6b.TABLE, chunk_size_rows=16, expected=facts, native_threads=1
    )
    err = b6b.fails_with(ExecutionError, lambda: list(inputs.chunks))
    assert getattr(err, "code", None) == "lazy_source_row_count_mismatch"


# ---------------------------------------------------------------------------
# Test 4: bounded input, structurally.
# ---------------------------------------------------------------------------


def _allocation_peak(
    tmp_path: Path, rows: int, monkeypatch: pytest.MonkeyPatch, *, lazy: bool
) -> int:
    mod = b6b.b6a.sink_module()
    monkeypatch.setattr(mod, "ROW_GROUP_ROWS", 20_000)
    cfg, path = b6b.stream_job(tmp_path, rows, 20_000)
    gc.collect()
    base = pa.total_allocated_bytes()
    samples: list[int] = []
    sink = b6b.CountingSink(
        on_batch=lambda _t, _b: samples.append(pa.total_allocated_bytes() - base)
    )
    source: Any = b6b.lazy(path) if lazy else pq.read_table(path)
    b6b.run(cfg, {b6b.TABLE: source}, sink, chunk_size_rows=5_000, auto_chunk_threshold_rows=1_000)
    assert samples, "the run must stream into the sink"
    assert sink.rows == rows
    return max(samples)


def test_the_input_working_set_is_flat_in_rows_and_the_twin_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_dir, big_dir = tmp_path / "small", tmp_path / "big"
    small_dir.mkdir()
    big_dir.mkdir()
    lazy_small = _allocation_peak(small_dir, 200_000, monkeypatch, lazy=True)
    lazy_big = _allocation_peak(big_dir, 1_000_000, monkeypatch, lazy=True)
    assert lazy_big <= 1.25 * lazy_small + 4 * MIB, (lazy_small, lazy_big)
    # Red control: with the whole table resident the same check must fail.
    twin_small = _allocation_peak(small_dir, 200_000, monkeypatch, lazy=False)
    twin_big = _allocation_peak(big_dir, 1_000_000, monkeypatch, lazy=False)
    assert not (twin_big <= 1.25 * twin_small + 4 * MIB), (twin_small, twin_big)


def test_the_lane_never_pulls_a_whole_file_and_keeps_the_resident_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, path = b6b.stream_job(tmp_path, 12_000, 4_000)
    spies = b6b.ReadSpies(monkeypatch)
    sink = b6b.CountingSink()
    b6b.run(
        cfg,
        {b6b.TABLE: b6b.lazy(path)},
        sink,
        chunk_size_rows=5_000,
        auto_chunk_threshold_rows=1_000,
    )
    assert spies.none_called()
    assert sink.rows == 12_000

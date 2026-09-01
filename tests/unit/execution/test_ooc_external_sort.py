"""OOC-B milestone 1, task 2: `BoundedExternalSorter`.

Correctness first (this file's Step 1 tests): a shuffled stream of batches
comes back ordered by `row_nr`, a single row wider than the sorter's byte cap
fails closed instead of silently blowing the cap, and `close()` leaves no
run files behind. The byte-cap instrumentation tests (peak_buffered_bytes,
bounded merge heads, bounded emitted batches) are appended below these.
"""

from __future__ import annotations

import random

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._external_sort import (
    SORT_OVERHEAD_FACTOR,
    BoundedExternalSorter,
)

ROW_NR = "__decoy_row_nr"


def _batches_from_row_nrs(row_nrs: list[int], payload_widths: list[int], batch_size: int):
    """Build shuffled batches: `row_nr` int64 column plus a variable-width
    binary `payload` column sized per `payload_widths[i]`."""
    batches = []
    for start in range(0, len(row_nrs), batch_size):
        chunk_nrs = row_nrs[start : start + batch_size]
        chunk_widths = payload_widths[start : start + batch_size]
        payloads = [b"p" * w for w in chunk_widths]
        batches.append(
            pa.record_batch(
                {
                    ROW_NR: pa.array(chunk_nrs, type=pa.int64()),
                    "payload": pa.array(payloads, type=pa.binary()),
                }
            )
        )
    return batches


def test_shuffled_input_is_sorted_by_row_nr(tmp_path):
    n = 5_000
    row_nrs = list(range(n))
    random.Random(1234).shuffle(row_nrs)
    widths = [8] * n
    batches = _batches_from_row_nrs(row_nrs, widths, batch_size=137)

    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "spill",
        run_bytes_cap=64 * 1024,
        merge_fan_in=4,
    )
    try:
        for batch in batches:
            sorter.write(batch)
        sorter.finish()
        seen = [
            value
            for out_batch in sorter.iter_ordered()
            for value in out_batch.column(ROW_NR).to_pylist()
        ]
        assert seen == list(range(n))
    finally:
        sorter.close()


def test_row_wider_than_cap_fails_closed(tmp_path):
    cap = 4 * 1024
    wide_payload = b"x" * (cap * 4)
    batch = pa.record_batch(
        {
            ROW_NR: pa.array([0, 1, 2], type=pa.int64()),
            "payload": pa.array([b"a", wide_payload, b"c"], type=pa.binary()),
        }
    )
    sorter = BoundedExternalSorter(spill_dir=tmp_path / "spill", run_bytes_cap=cap, merge_fan_in=4)
    try:
        with pytest.raises(ExecutionError) as exc_info:
            sorter.write(batch)
        assert exc_info.value.code == "out_of_core_sort_row_too_wide"
    finally:
        sorter.close()


def test_close_removes_run_files(tmp_path):
    spill_dir = tmp_path / "spill"
    n = 2_000
    row_nrs = list(range(n))
    random.Random(99).shuffle(row_nrs)
    widths = [16] * n
    batches = _batches_from_row_nrs(row_nrs, widths, batch_size=97)

    sorter = BoundedExternalSorter(spill_dir=spill_dir, run_bytes_cap=16 * 1024, merge_fan_in=4)
    for batch in batches:
        sorter.write(batch)
    sorter.finish()
    assert any(spill_dir.iterdir())
    sorter.close()
    assert list(spill_dir.iterdir()) == []
    # Idempotent: calling close() again must not raise.
    sorter.close()


# --- Byte-cap instrumentation (the core proof; guide Step 4) -----------------


def test_buffer_never_exceeds_cap_wide_variable_rows(tmp_path):
    n = 4_000
    row_nrs = list(range(n))
    rng = random.Random(42)
    rng.shuffle(row_nrs)
    # Highly variable width: mostly tiny, some large binary cells, so total
    # size far exceeds run_bytes_cap and the buffer must flush repeatedly.
    widths = [rng.choice([8, 16, 4_000, 32]) for _ in range(n)]
    batches = _batches_from_row_nrs(row_nrs, widths, batch_size=53)

    # cap chosen so the 4000-byte rows sit under the per-merge-head cap
    # (run_bytes_cap // (2 * fan_in) = 8192 here), which write() now enforces.
    run_bytes_cap = 64 * 1024
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "spill", run_bytes_cap=run_bytes_cap, merge_fan_in=4
    )
    try:
        for batch in batches:
            sorter.write(batch)
        sorter.finish()
        # Drain to prove the sort is still correct at this scale/shape.
        seen = [
            value
            for out_batch in sorter.iter_ordered()
            for value in out_batch.column(ROW_NR).to_pylist()
        ]
        assert seen == list(range(n))
        # Bracketed, not just an upper bound: total bytes far exceed the cap, so
        # the buffer fills close to the cap before each flush. A lower bound kills
        # an instrumentation mutation that undercounts the peak toward zero (which
        # would make `peak <= cap` vacuously pass).
        assert run_bytes_cap // 2 <= sorter.peak_pre_sort_buffer_bytes <= run_bytes_cap
        assert (
            sorter.peak_pre_sort_buffer_bytes
            <= sorter.peak_buffered_bytes
            <= run_bytes_cap * SORT_OVERHEAD_FACTOR
        )
    finally:
        sorter.close()


def test_merge_resident_within_cap(tmp_path):
    # Tiny cap + large N forces many runs and a real multi-run, multi-pass merge.
    n = 6_000
    row_nrs = list(range(n))
    random.Random(7).shuffle(row_nrs)
    widths = [24] * n
    batches = _batches_from_row_nrs(row_nrs, widths, batch_size=31)

    run_bytes_cap = 8 * 1024
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "spill", run_bytes_cap=run_bytes_cap, merge_fan_in=4
    )
    try:
        for batch in batches:
            sorter.write(batch)
        # More than merge_fan_in runs must have been created to prove a real
        # multi-pass merge is exercised, not a single trivial merge.
        assert len(list((tmp_path / "spill").glob("run_*.arrow"))) > 4
        sorter.finish()
        seen = [
            value
            for out_batch in sorter.iter_ordered()
            for value in out_batch.column(ROW_NR).to_pylist()
        ]
        assert seen == list(range(n))
        # Bracketed: the multi-pass merge really loads heads, so the peak is a
        # real positive measurement (kills a zero-undercount instrumentation
        # mutation), and it stays within the cap.
        assert 0 < sorter.peak_merge_resident_bytes <= run_bytes_cap
    finally:
        sorter.close()


def test_emitted_batches_bounded(tmp_path):
    n = 3_000
    row_nrs = list(range(n))
    random.Random(5).shuffle(row_nrs)
    widths = [64] * n
    batches = _batches_from_row_nrs(row_nrs, widths, batch_size=41)

    run_bytes_cap = 16 * 1024
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "spill", run_bytes_cap=run_bytes_cap, merge_fan_in=4
    )
    try:
        for batch in batches:
            sorter.write(batch)
        sorter.finish()
        emitted = list(sorter.iter_ordered())
        assert emitted, "expected at least one emitted batch"
        for out_batch in emitted:
            assert out_batch.nbytes <= run_bytes_cap
    finally:
        sorter.close()

"""Handle lifecycle of the out-of-core spill readers (no reliance on GC).

The readers open their spill through `pa.OSFile`; a leaked handle would pin a
file descriptor (and, on the old mapped readers, the mapping). Open descriptors
onto the spill path are counted through `/proc/self/fd`, so each path is
checked deterministically: normal exhaustion, early close, exception mid-read,
repeated opens from row 0, DuckDB registration (including a register failure),
and the lockstep cursor.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from decoy_engine.execution.out_of_core._payload_store import (
    RawParentKeySpill,
    SpillChildKeys,
    SpillPayloadStore,
)
from decoy_engine.execution.out_of_core._stream_join_cursors import ChildKeyLockstepCursor

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="counts descriptors via /proc/self/fd"
)

_SCHEMA = pa.schema([("__decoy_row_nr", pa.int64())])


def _open_fds(path: Path) -> int:
    n = 0
    for entry in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{entry}") == str(path):
                n += 1
        except OSError:
            continue
    return n


def _batches() -> list[pa.RecordBatch]:
    return [
        pa.record_batch({"__decoy_row_nr": pa.array([0, 1], type=pa.int64())}),
        pa.record_batch({"__decoy_row_nr": pa.array([2, 3, 4], type=pa.int64())}),
    ]


@pytest.fixture
def keys(tmp_path: Path) -> SpillChildKeys:
    spill = SpillChildKeys(tmp_path / "child_keys.arrow", _SCHEMA)
    for b in _batches():
        spill.append(b)
    spill.finalize()
    return spill


def _rows(reader: pa.RecordBatchReader) -> list[int]:
    return [v for b in reader for v in b.column(0).to_pylist()]


def test_open_reader_yields_real_reader_open_inside_closed_after(keys: SpillChildKeys) -> None:
    path = keys._path
    with keys.open_reader() as reader:
        assert isinstance(reader, pa.RecordBatchReader)
        assert _open_fds(path) == 1
        assert _rows(reader) == [0, 1, 2, 3, 4]
        assert _open_fds(path) == 1  # exhaustion alone does not close the handle
    assert _open_fds(path) == 0


def test_open_reader_repeated_opens_each_start_at_row_zero(keys: SpillChildKeys) -> None:
    for _ in range(3):
        with keys.open_reader() as reader:
            assert _rows(reader) == [0, 1, 2, 3, 4]
        assert _open_fds(keys._path) == 0


def test_open_reader_early_exit_closes_and_next_open_restarts(keys: SpillChildKeys) -> None:
    with keys.open_reader() as reader:
        next(reader)
    assert _open_fds(keys._path) == 0
    with keys.open_reader() as reader:
        assert _rows(reader) == [0, 1, 2, 3, 4]


def test_open_reader_exception_mid_read_closes_handle(keys: SpillChildKeys) -> None:
    with pytest.raises(RuntimeError, match="boom"), keys.open_reader() as reader:
        next(reader)
        raise RuntimeError("boom")
    assert _open_fds(keys._path) == 0


def test_open_reader_before_finalize_raises_without_opening(tmp_path: Path) -> None:
    spill = SpillChildKeys(tmp_path / "ck.arrow", _SCHEMA)
    writer_fds = _open_fds(spill._path)  # the still-open writer's own descriptor
    with pytest.raises(AssertionError, match="before finalize"), spill.open_reader():
        pass
    assert _open_fds(spill._path) == writer_fds
    spill.finalize()


def test_open_reader_construction_failure_closes_osfile(tmp_path: Path) -> None:
    spill = SpillChildKeys(tmp_path / "ck.arrow", _SCHEMA)
    spill.finalize()
    spill._path.write_bytes(b"not an arrow stream")
    with pytest.raises(pa.ArrowInvalid), spill.open_reader():
        pass
    assert _open_fds(spill._path) == 0


def test_duckdb_registration_path_closes_after_unregister(keys: SpillChildKeys) -> None:
    conn = duckdb.connect()
    try:
        with keys.open_reader() as reader:
            conn.register("child_keys", reader)
            try:
                assert _open_fds(keys._path) == 1
                count = conn.execute("SELECT count(*) FROM child_keys").fetchone()[0]
            finally:
                conn.unregister("child_keys")
        assert count == 5
        assert _open_fds(keys._path) == 0
        with keys.open_reader() as reader:  # second scan needs its own reader
            conn.register("child_keys", reader)
            try:
                assert conn.execute("SELECT count(*) FROM child_keys").fetchone()[0] == 5
            finally:
                conn.unregister("child_keys")
    finally:
        conn.close()
    assert _open_fds(keys._path) == 0


def test_duckdb_register_failure_closes_handle(keys: SpillChildKeys) -> None:
    conn = duckdb.connect()
    conn.close()
    with pytest.raises(duckdb.Error), keys.open_reader() as reader:
        conn.register("child_keys", reader)
    assert _open_fds(keys._path) == 0


def test_lockstep_cursor_consumes_then_context_closes(keys: SpillChildKeys) -> None:
    with keys.open_reader() as reader:
        cursor = ChildKeyLockstepCursor(reader)
        cursor.take(2, expected_row_nr_start=0)
        cursor.take(3, expected_row_nr_start=2)
        cursor.assert_exhausted()
        assert _open_fds(keys._path) == 1
        cursor.close()  # the driver closes the cursor, then the stack exits the context
    assert _open_fds(keys._path) == 0


def test_lockstep_cursor_exception_mid_read_closes_handle(keys: SpillChildKeys) -> None:
    with pytest.raises(RuntimeError, match="boom"), keys.open_reader() as reader:
        cursor = ChildKeyLockstepCursor(reader)
        cursor.take(2, expected_row_nr_start=0)
        raise RuntimeError("boom")
    assert _open_fds(keys._path) == 0


def _payload_store(tmp_path: Path) -> SpillPayloadStore:
    store = SpillPayloadStore(tmp_path / "payload.arrow")
    for b in _batches():
        store.append(b)
    return store


def test_payload_store_iter_batches_exhaust_and_abandon(tmp_path: Path) -> None:
    store = _payload_store(tmp_path)
    path = tmp_path / "payload.arrow"
    gen = store.iter_batches()
    next(gen)
    assert _open_fds(path) == 1
    gen.close()
    assert _open_fds(path) == 0
    assert [off for off, _ in store.iter_batches()] == [0, 2]  # restarts at row 0
    assert _open_fds(path) == 0
    store.close()


def test_payload_store_iter_batches_exception_closes_handle(tmp_path: Path) -> None:
    store = _payload_store(tmp_path)
    gen = store.iter_batches()
    next(gen)
    with pytest.raises(RuntimeError, match="boom"):
        gen.throw(RuntimeError("boom"))
    assert _open_fds(tmp_path / "payload.arrow") == 0


def test_raw_parent_key_spill_exhaust_abandon_and_reopen(tmp_path: Path) -> None:
    spill = RawParentKeySpill(tmp_path / "raw.arrow", _SCHEMA)
    for b in _batches():
        spill.append(b)
    spill.finalize()
    it = iter(spill)
    next(it)
    assert _open_fds(spill._path) == 1
    it.close()  # type: ignore[attr-defined]
    assert _open_fds(spill._path) == 0
    assert sum(b.num_rows for b in spill) == 5
    assert sum(b.num_rows for b in spill) == 5  # re-readable from row 0
    assert _open_fds(spill._path) == 0

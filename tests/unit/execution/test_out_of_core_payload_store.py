"""OOC-B single-read redesign: the masked-payload store (`_payload_store.py`).

Phase 1 of the single-read driver captures each masked source batch into this
store, keyed by its `__decoy_row_nr` offset; phase 3 resolves FK columns from
the store instead of re-reading the source. Both implementations (resident
list, Arrow-IPC spill) must be lossless and report identical running offsets.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution.out_of_core._payload_store import (
    RawParentKeySpill,
    ResidentPayloadStore,
    SpillPayloadStore,
)


def _batches() -> list[pa.RecordBatch]:
    return [
        pa.record_batch({"a": pa.array([1, 2], pa.int64()), "b": pa.array(["x", "y"])}),
        pa.record_batch({"a": pa.array([3], pa.int64()), "b": pa.array(["z"])}),
    ]


def _make_store(factory: str, tmp_path):
    return (
        ResidentPayloadStore() if factory == "resident" else SpillPayloadStore(tmp_path / "p.arrow")
    )


@pytest.mark.parametrize("factory", ["resident", "spill"])
def test_store_roundtrip_lossless_with_offsets(tmp_path, factory) -> None:
    store = _make_store(factory, tmp_path)
    for b in _batches():
        store.append(b)
    got = list(store.iter_batches())
    store.close()
    assert [off for off, _ in got] == [0, 2]
    assert got[0][1].schema == _batches()[0].schema
    assert got[0][1].column("a").to_pylist() == [1, 2]
    assert got[1][1].column("b").to_pylist() == ["z"]


@pytest.mark.parametrize("factory", ["resident", "spill"])
def test_empty_store_yields_nothing(tmp_path, factory) -> None:
    store = _make_store(factory, tmp_path)
    assert list(store.iter_batches()) == []
    store.close()


@pytest.mark.parametrize("factory", ["resident", "spill"])
def test_zero_row_batches_do_not_shift_offsets(tmp_path, factory) -> None:
    # A zero-row batch must not consume an offset slot: appending one between
    # two real batches must not shift the second batch's running row_nr.
    store = _make_store(factory, tmp_path)
    schema = pa.schema([pa.field("a", pa.int64())])
    store.append(pa.record_batch({"a": pa.array([1, 2], pa.int64())}))
    store.append(pa.record_batch([pa.array([], type=pa.int64())], schema=schema))
    store.append(pa.record_batch({"a": pa.array([3], pa.int64())}))
    got = list(store.iter_batches())
    store.close()
    assert [off for off, _ in got] == [0, 2]


@pytest.mark.parametrize("factory", ["resident", "spill"])
def test_multiple_columns_and_types_preserved(tmp_path, factory) -> None:
    store = _make_store(factory, tmp_path)
    batch = pa.record_batch(
        {
            "i": pa.array([1, None, 3], pa.int64()),
            "f": pa.array([1.5, 2.5, None], pa.float64()),
            "s": pa.array(["a", None, "c"]),
            "bo": pa.array([True, False, None]),
        }
    )
    store.append(batch)
    got = list(store.iter_batches())
    store.close()
    assert len(got) == 1
    off, out_batch = got[0]
    assert off == 0
    assert out_batch.schema == batch.schema
    assert out_batch.equals(batch)


def test_raw_parent_key_spill_roundtrips_parquet_hostile_types(tmp_path) -> None:
    # The raw-key spill must be Arrow IPC, not Parquet: an FK key column may
    # carry an Arrow type Parquet cannot encode (month_day_nano_interval,
    # run-end-encoded) yet the route admits and the oracle masks. A Parquet
    # spill would raise ArrowNotImplementedError here; IPC round-trips it.
    schema = pa.schema(
        [
            pa.field("interval", pa.month_day_nano_interval()),
            pa.field("ree", pa.run_end_encoded(pa.int32(), pa.int64())),
        ]
    )
    batch = pa.record_batch(
        [
            pa.array(
                [pa.MonthDayNano([1, 2, 3]), pa.MonthDayNano([4, 5, 6])],
                type=pa.month_day_nano_interval(),
            ),
            pa.RunEndEncodedArray.from_arrays(
                pa.array([2], pa.int32()), pa.array([10], pa.int64())
            ),
        ],
        schema=schema,
    )
    spill = RawParentKeySpill(tmp_path / "raw_keys.arrow", schema)
    spill.append(batch)
    spill.finalize()
    got = list(spill)
    assert len(got) == 1
    assert got[0].schema == schema
    assert got[0].equals(batch)


def test_raw_parent_key_spill_is_rereadable_and_bounded(tmp_path) -> None:
    # The outgoing-relation build reads `source_parent` once PER outgoing edge,
    # so the spill must re-read on each __iter__ (a single-pass iterable would
    # break a table with two outgoing edges). Zero-row batches are skipped.
    schema = pa.schema([pa.field("k", pa.int64())])
    spill = RawParentKeySpill(tmp_path / "keys.arrow", schema)
    spill.append(pa.record_batch({"k": pa.array([1, 2], pa.int64())}))
    spill.append(pa.record_batch([pa.array([], type=pa.int64())], schema=schema))
    spill.append(pa.record_batch({"k": pa.array([3], pa.int64())}))
    spill.finalize()
    first = [b.column("k").to_pylist() for b in spill]
    second = [b.column("k").to_pylist() for b in spill]
    assert first == second == [[1, 2], [3]]


def test_raw_parent_key_spill_empty_and_idempotent_finalize(tmp_path) -> None:
    schema = pa.schema([pa.field("k", pa.int64())])
    spill = RawParentKeySpill(tmp_path / "empty.arrow", schema)
    spill.finalize()
    spill.finalize()  # idempotent: the finally-block guard must not double-fault
    assert list(spill) == []

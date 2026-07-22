"""`SpillChildKeys` (`_payload_store.py`): the disk-backed, re-openable
child-key store that replaces `StreamFkJoiner`'s `child_keys` TEMP TABLE
(OOC-B fix#1b). The two properties this file pins: `open_reader()` returns a
FRESH single-pass reader on every call (the reopen-per-scan requirement a
DuckDB-registered `RecordBatchReader` demands), and the IPC format round-trips
every admitted key type, including one Parquet cannot encode.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.execution.out_of_core._payload_store import SpillChildKeys

_SCHEMA = pa.schema(
    [
        pa.field("__decoy_row_nr", pa.int64()),
        pa.field("__decoy_fk_join_key", pa.string()),
        pa.field("__decoy_src_0", pa.string()),
    ]
)


def _batch(row_nr_start: int, keys: list[str]) -> pa.RecordBatch:
    n = len(keys)
    return pa.record_batch(
        [
            pa.array(range(row_nr_start, row_nr_start + n), type=pa.int64()),
            pa.array(keys, type=pa.string()),
            pa.array(keys, type=pa.string()),
        ],
        schema=_SCHEMA,
    )


def test_open_reader_twice_both_return_the_full_row_set(tmp_path) -> None:
    # The reopen-per-scan requirement: StreamFkJoiner scans the child TWICE
    # (total_orphans's FAIL precount, then iter_join_rows's join), and a
    # registered RecordBatchReader is single-pass -- a shared reader would
    # return the full set on the first scan and zero rows on the second.
    spill = SpillChildKeys(tmp_path / "child_keys.arrow", _SCHEMA)
    spill.append(_batch(0, ["a", "b", "c"]))
    spill.append(_batch(3, ["d", "e"]))
    spill.finalize()

    first = spill.open_reader()
    try:
        first_rows = pa.Table.from_batches(list(first), schema=_SCHEMA)
    finally:
        first.close()
    second = spill.open_reader()
    try:
        second_rows = pa.Table.from_batches(list(second), schema=_SCHEMA)
    finally:
        second.close()

    assert first_rows.num_rows == 5
    assert second_rows.num_rows == 5
    assert first_rows.equals(second_rows)
    assert first_rows.column("__decoy_row_nr").to_pylist() == [0, 1, 2, 3, 4]
    assert first_rows.column("__decoy_fk_join_key").to_pylist() == ["a", "b", "c", "d", "e"]


def test_early_aborted_read_then_fresh_open_reader_yields_full_set(tmp_path) -> None:
    # A consumer that stops partway through one reader (e.g. a FAIL-policy
    # short-circuit) must not leave the NEXT open_reader() call starting
    # mid-stream: each call is independent, always from byte 0.
    spill = SpillChildKeys(tmp_path / "child_keys.arrow", _SCHEMA)
    spill.append(_batch(0, ["a", "b", "c", "d", "e"]))
    spill.finalize()

    aborted = spill.open_reader()
    next(iter(aborted))  # consume exactly one batch/row, then abandon
    aborted.close()

    fresh = spill.open_reader()
    try:
        rows = pa.Table.from_batches(list(fresh), schema=_SCHEMA)
    finally:
        fresh.close()
    assert rows.num_rows == 5
    assert rows.column("__decoy_fk_join_key").to_pylist() == ["a", "b", "c", "d", "e"]


def test_empty_spill_reads_back_as_no_batches(tmp_path) -> None:
    spill = SpillChildKeys(tmp_path / "child_keys.arrow", _SCHEMA)
    spill.finalize()
    reader = spill.open_reader()
    try:
        assert list(reader) == []
    finally:
        reader.close()


def test_zero_row_batches_are_skipped(tmp_path) -> None:
    spill = SpillChildKeys(tmp_path / "child_keys.arrow", _SCHEMA)
    spill.append(_batch(0, ["a"]))
    spill.append(pa.record_batch([pa.array([], type=f.type) for f in _SCHEMA], schema=_SCHEMA))
    spill.append(_batch(1, ["b"]))
    spill.finalize()
    reader = spill.open_reader()
    try:
        rows = pa.Table.from_batches(list(reader), schema=_SCHEMA)
    finally:
        reader.close()
    assert rows.column("__decoy_fk_join_key").to_pylist() == ["a", "b"]


def test_finalize_is_idempotent(tmp_path) -> None:
    spill = SpillChildKeys(tmp_path / "child_keys.arrow", _SCHEMA)
    spill.append(_batch(0, ["a"]))
    spill.finalize()
    spill.finalize()  # must not raise or double-close
    reader = spill.open_reader()
    try:
        rows = pa.Table.from_batches(list(reader), schema=_SCHEMA)
    finally:
        reader.close()
    assert rows.num_rows == 1


def test_roundtrips_a_parquet_hostile_admitted_key_type(tmp_path) -> None:
    # month_day_nano_interval is Arrow-legal, admitted by the FK route, and
    # masked by the oracle, but Parquet cannot encode it -- the reason this
    # spill is IPC, not Parquet (mirrors
    # test_out_of_core_payload_store.py::test_raw_parent_key_spill_roundtrips_parquet_hostile_types).
    schema = pa.schema(
        [
            pa.field("__decoy_row_nr", pa.int64()),
            pa.field("__decoy_fk_join_key", pa.string()),
            pa.field("__decoy_src_0", pa.month_day_nano_interval()),
        ]
    )
    batch = pa.record_batch(
        [
            pa.array([0, 1], type=pa.int64()),
            pa.array(["k0", "k1"], type=pa.string()),
            pa.array(
                [pa.MonthDayNano([1, 2, 3]), pa.MonthDayNano([4, 5, 6])],
                type=pa.month_day_nano_interval(),
            ),
        ],
        schema=schema,
    )
    spill = SpillChildKeys(tmp_path / "child_keys.arrow", schema)
    spill.append(batch)
    spill.finalize()
    reader = spill.open_reader()
    try:
        got = pa.Table.from_batches(list(reader), schema=schema)
    finally:
        reader.close()
    assert got.schema == schema
    assert got.equals(pa.Table.from_batches([batch]))


def test_append_after_finalize_raises() -> None:
    spill = SpillChildKeys.__new__(SpillChildKeys)  # avoid touching the filesystem
    spill._writer = None
    try:
        spill.append(_batch(0, ["a"]))
    except AssertionError as exc:
        assert "finalize" in str(exc)
    else:
        raise AssertionError("expected append-after-finalize to raise")


def test_open_reader_before_finalize_raises(tmp_path) -> None:
    spill = SpillChildKeys(tmp_path / "child_keys.arrow", _SCHEMA)
    try:
        spill.open_reader()
    except AssertionError as exc:
        assert "finalize" in str(exc)
    else:
        raise AssertionError("expected open_reader-before-finalize to raise")
    finally:
        spill.finalize()

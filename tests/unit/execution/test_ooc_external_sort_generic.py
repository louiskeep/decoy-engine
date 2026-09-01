"""P4-A.2: the key-generalization of `BoundedExternalSorter`.

The ported `test_ooc_external_sort.py` proves the int-`row_nr` behavior + the
memory-cap envelope. This file proves the generalization: parity for every
allowlisted key type against an in-memory `sort_by` oracle, the string key
through a forced multi-pass merge across caps/fan-ins, partition-invariance,
the fail-closed key contract, and the failure-cleanup registry.
"""

from __future__ import annotations

import random
import signal

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._external_sort import BoundedExternalSorter

KEY = "k"


def _key_array(kind: str, values: list[int]) -> pa.Array:
    """A UNIQUE, order-preserving key array of the requested type from `values`
    (non-negative ints). The natural Arrow order of the result matches the
    ascending int order, so an in-memory `sort_by(KEY)` is a clean oracle."""
    if kind == "int64":
        return pa.array(values, type=pa.int64())
    if kind == "uint32":
        return pa.array(values, type=pa.uint32())
    if kind == "string":
        return pa.array([f"{v:010d}" for v in values], type=pa.string())
    if kind == "large_string":
        return pa.array([f"{v:010d}" for v in values], type=pa.large_string())
    if kind == "binary":
        return pa.array([v.to_bytes(8, "big") for v in values], type=pa.binary())
    if kind == "large_binary":
        return pa.array([v.to_bytes(8, "big") for v in values], type=pa.large_binary())
    if kind == "date32":
        return pa.array(values, type=pa.date32())
    if kind == "date64":
        # date64 is ms since epoch; use whole-day multiples so values are valid.
        return pa.array([v * 86_400_000 for v in values], type=pa.date64())
    if kind.startswith("timestamp_"):
        return pa.array(values, type=pa.timestamp(kind.split("_", 1)[1]))
    raise AssertionError(kind)


def _batches(kind: str, values: list[int], batch_size: int) -> list[pa.RecordBatch]:
    """Shuffled batches: a `KEY` column of the given type plus a `payload`
    binary column tied to the key value (so a wrong reorder shows up)."""
    order = list(range(len(values)))
    random.Random(len(values)).shuffle(order)
    shuffled = [values[i] for i in order]
    out = []
    for start in range(0, len(shuffled), batch_size):
        chunk = shuffled[start : start + batch_size]
        out.append(
            pa.record_batch(
                {
                    KEY: _key_array(kind, chunk),
                    "payload": pa.array([v.to_bytes(8, "big") for v in chunk], type=pa.binary()),
                }
            )
        )
    return out


def _sorted(batches, tmp_path, cap, fan_in, name="spill") -> pa.Table:
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / name, run_bytes_cap=cap, merge_fan_in=fan_in, sort_key_column=KEY
    )
    try:
        for b in batches:
            sorter.write(b)
        sorter.finish()
        return pa.Table.from_batches(list(sorter.iter_ordered()), schema=batches[0].schema)
    finally:
        sorter.close()


ALLOWLISTED = [
    "int64",
    "uint32",
    "string",
    "large_string",
    "binary",
    "large_binary",
    "date32",
    "date64",
    "timestamp_s",
    "timestamp_ms",
    "timestamp_us",
    "timestamp_ns",  # the unit that hung the merge before the scalar-cutoff fix
]


@pytest.mark.parametrize("kind", ALLOWLISTED)
def test_positive_parity_per_allowlisted_key_type(kind, tmp_path):
    # One cheap single-run case per promised key type: value+schema parity to
    # the in-memory sort_by oracle.
    n = 500
    values = list(range(n))
    batches = _batches(kind, values, batch_size=37)
    oracle = pa.Table.from_batches(batches).sort_by(KEY)
    out = _sorted(batches, tmp_path, cap=256 * 1024, fan_in=4)
    assert out.schema == oracle.schema
    assert out.to_pydict() == oracle.to_pydict()


@pytest.mark.parametrize("cap", [2 * 1024, 4 * 1024])
@pytest.mark.parametrize("fan_in", [2, 4])
def test_string_key_multipass_parity(cap, fan_in, tmp_path):
    # The generic multi-pass cutoff logic lives only in the merge; force
    # > merge_fan_in runs and compare full value+schema across caps x fan-ins.
    # Small n + tiny caps still makes many runs (so > fan_in for every combo),
    # keeping the multi-pass coverage fast enough to mutation-grade.
    n = 1_500
    values = list(range(n))
    batches = _batches("string", values, batch_size=31)
    oracle = pa.Table.from_batches(batches).sort_by(KEY)
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "spill", run_bytes_cap=cap, merge_fan_in=fan_in, sort_key_column=KEY
    )
    try:
        for b in batches:
            sorter.write(b)
        assert len(list((tmp_path / "spill").glob("run_*.arrow"))) > fan_in
        sorter.finish()
        out = pa.Table.from_batches(list(sorter.iter_ordered()), schema=batches[0].schema)
    finally:
        sorter.close()
    assert out.schema == oracle.schema
    assert out.to_pydict() == oracle.to_pydict()


def test_timestamp_ns_key_multipass_does_not_hang(tmp_path):
    # Regression for the ns-timestamp merge hang: `pc.max().as_py()` returned a
    # pandas.Timestamp that `pc.less_equal` truncated to us, wedging the merge.
    # A ns key through a forced multi-pass merge (tiny cap) must terminate AND
    # match the oracle. GUARDED by SIGALRM: if the bug regresses, the alarm fires
    # and the test FAILS instead of wedging the whole CI job.
    n = 1_500
    values = list(range(n))
    batches = _batches("timestamp_ns", values, batch_size=31)
    oracle = pa.Table.from_batches(batches).sort_by(KEY)
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "spill", run_bytes_cap=2 * 1024, merge_fan_in=2, sort_key_column=KEY
    )

    class _HangError(Exception):
        pass

    def _on_alarm(signum, frame):
        raise _HangError

    has_alarm = hasattr(signal, "SIGALRM")
    if has_alarm:
        old = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(30)
    try:
        for b in batches:
            sorter.write(b)
        assert len(list((tmp_path / "spill").glob("run_*.arrow"))) > 2  # multi-pass
        sorter.finish()
        out = pa.Table.from_batches(list(sorter.iter_ordered()), schema=batches[0].schema)
        assert out.to_pydict() == oracle.to_pydict()
    except _HangError:
        pytest.fail("ns-key merge did not terminate within 30s (scalar-cutoff fix regressed)")
    finally:
        if has_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        sorter.close()


def test_duplicate_keys_sort_by_key_multiset_preserved(tmp_path):
    # Duplicate keys sort correctly BY KEY (non-decreasing) and lose no rows;
    # the tie order among equal keys is unspecified, so compare as a multiset.
    n = 1_500
    values = [i % 50 for i in range(n)]  # heavy duplication
    batches = _batches("int64", values, batch_size=53)
    out = _sorted(batches, tmp_path, cap=2 * 1024, fan_in=3)
    keys = out.column(KEY).to_pylist()
    assert keys == sorted(keys)  # non-decreasing by key
    in_rows = sorted(
        zip(
            pa.Table.from_batches(batches).column(KEY).to_pylist(),
            pa.Table.from_batches(batches).column("payload").to_pylist(),
            strict=True,
        )
    )
    out_rows = sorted(zip(keys, out.column("payload").to_pylist(), strict=True))
    assert out_rows == in_rows  # no row lost or duplicated


def test_partition_invariance_tiny_cap_equals_huge_cap(tmp_path):
    # The emitted order is independent of the cap / fan-in for a unique key.
    n = 1_500
    values = list(range(n))
    batches = _batches("string", values, batch_size=29)
    tiny = _sorted(batches, tmp_path, cap=2 * 1024, fan_in=2, name="tiny")
    huge = _sorted(batches, tmp_path, cap=64 * 1024 * 1024, fan_in=8, name="huge")
    assert tiny.to_pydict() == huge.to_pydict()
    assert tiny.schema == huge.schema


# --- Fail-closed key contract (validated before any spill) -------------------


def _assert_rejects(tmp_path, batch, code):
    spill = tmp_path / "spill"
    sorter = BoundedExternalSorter(
        spill_dir=spill, run_bytes_cap=64 * 1024, merge_fan_in=4, sort_key_column=KEY
    )
    try:
        with pytest.raises(ExecutionError) as exc:
            sorter.write(batch)
        assert exc.value.code == code
        # Validation runs before buffering/spill: no run file was created.
        assert list(spill.glob("*.arrow")) == []
    finally:
        sorter.close()


def test_reject_null_key(tmp_path):
    batch = pa.record_batch(
        {KEY: pa.array([0, None, 2], type=pa.int64()), "payload": pa.array([b"a", b"b", b"c"])}
    )
    _assert_rejects(tmp_path, batch, "out_of_core_sort_key_null")


def test_reject_float_key(tmp_path):
    batch = pa.record_batch(
        {KEY: pa.array([0.0, 1.0, 2.0], type=pa.float64()), "payload": pa.array([b"a", b"b", b"c"])}
    )
    _assert_rejects(tmp_path, batch, "out_of_core_sort_key_type_unsupported")


def test_reject_unsupported_key_type(tmp_path):
    batch = pa.record_batch(
        {
            KEY: pa.array([True, False, True], type=pa.bool_()),
            "payload": pa.array([b"a", b"b", b"c"]),
        }
    )
    _assert_rejects(tmp_path, batch, "out_of_core_sort_key_type_unsupported")


def test_reject_missing_key_column(tmp_path):
    batch = pa.record_batch(
        {"other": pa.array([0, 1, 2], type=pa.int64()), "payload": pa.array([b"a", b"b", b"c"])}
    )
    _assert_rejects(tmp_path, batch, "out_of_core_sort_key_missing")


def test_reject_row_wider_than_per_head_cap(tmp_path):
    # A single row between the per-merge-head cap (run_bytes_cap // (2*fan_in))
    # and run_bytes_cap passes the OLD guard but would become an over-cap run
    # batch that the merge co-loads fan_in of, breaking the run_bytes_cap
    # envelope. It must fail closed. cap=8KB, fan_in=4 -> per-head cap=1024; a
    # ~3KB row is under run_bytes_cap but over the per-head cap.
    spill = tmp_path / "spill"
    sorter = BoundedExternalSorter(
        spill_dir=spill, run_bytes_cap=8 * 1024, merge_fan_in=4, sort_key_column=KEY
    )
    try:
        wide = pa.record_batch(
            {KEY: pa.array([0, 1], type=pa.int64()), "payload": pa.array([b"a", b"x" * 3000])}
        )
        with pytest.raises(ExecutionError) as exc:
            sorter.write(wide)
        assert exc.value.code == "out_of_core_sort_row_too_wide"
        assert list(spill.glob("*.arrow")) == []  # rejected before any spill
    finally:
        sorter.close()


def test_reject_narrow_rows_index_unbounded(tmp_path):
    # A schema whose narrowest row is under the 8-byte sort index (here a bare
    # int8 key, 1 byte/row) makes sort_by's index array dwarf the data the byte
    # cap counts, so it fails closed on the SCHEMA before any spill. int8 is an
    # allowlisted key type, so this passes key validation and is caught by the
    # schema min-row-width guard.
    spill = tmp_path / "spill"
    sorter = BoundedExternalSorter(
        spill_dir=spill, run_bytes_cap=64 * 1024, merge_fan_in=4, sort_key_column=KEY
    )
    try:
        narrow = pa.record_batch({KEY: pa.array(list(range(100)), type=pa.int8())})
        with pytest.raises(ExecutionError) as exc:
            sorter.write(narrow)
        assert exc.value.code == "out_of_core_sort_row_index_unbounded"
        assert list(spill.glob("*.arrow")) == []  # rejected before any spill
    finally:
        sorter.close()


def test_reject_narrow_key_only_int32(tmp_path):
    # A bare int32 key (4 bytes/row, < 8) is likewise rejected: the contract is
    # an effective row width >= 8 bytes, independent of which narrow key type.
    spill = tmp_path / "spill"
    sorter = BoundedExternalSorter(
        spill_dir=spill, run_bytes_cap=64 * 1024, merge_fan_in=4, sort_key_column=KEY
    )
    try:
        narrow = pa.record_batch({KEY: pa.array(list(range(100)), type=pa.int32())})
        with pytest.raises(ExecutionError) as exc:
            sorter.write(narrow)
        assert exc.value.code == "out_of_core_sort_row_index_unbounded"
    finally:
        sorter.close()


def test_reject_mixed_narrow_schema_that_could_cluster(tmp_path):
    # The guard is on the SCHEMA, not a per-batch average: an int8 key + binary
    # payload schema admits 5-byte rows (1-byte key + 4-byte offset + empty
    # data). A batch of mostly-empty payloads plus one wide payload has an
    # average row width far above 8 bytes, so an average check would ACCEPT it;
    # but after the flush sort clusters the empty-payload rows, a post-sort run
    # batch's 8-byte-per-row index would exceed its data. The schema guard
    # rejects it up front regardless of the batch's average.
    spill = tmp_path / "spill"
    sorter = BoundedExternalSorter(
        spill_dir=spill, run_bytes_cap=64 * 1024, merge_fan_in=4, sort_key_column=KEY
    )
    try:
        n = 100  # int8-valid key values
        payloads = [b""] * (n - 1) + [b"x" * 20_000]  # avg >> 8 bytes/row
        mixed = pa.record_batch(
            {
                KEY: pa.array(list(range(n)), type=pa.int8()),
                "payload": pa.array(payloads, type=pa.binary()),
            }
        )
        assert mixed.nbytes / mixed.num_rows > 8  # an average check would pass this
        with pytest.raises(ExecutionError) as exc:
            sorter.write(mixed)
        assert exc.value.code == "out_of_core_sort_row_index_unbounded"
        assert list(spill.glob("*.arrow")) == []
    finally:
        sorter.close()


def test_accept_int32_key_with_binary_payload(tmp_path):
    # The boundary is inclusive: int32 key (4) + binary payload (4-byte offset)
    # gives a schema min row of exactly 8 bytes, so every row clears the sort
    # index and the schema is accepted (and sorts correctly through a multi-pass
    # merge). Proves the schema guard over-rejects nothing at its boundary.
    n = 2_000
    values = list(range(n))
    order = list(range(n))
    random.Random(11).shuffle(order)
    shuffled = [values[i] for i in order]
    batches = [
        pa.record_batch(
            {
                KEY: pa.array(shuffled[s : s + 41], type=pa.int32()),
                "payload": pa.array([v.to_bytes(4, "big") for v in shuffled[s : s + 41]]),
            }
        )
        for s in range(0, n, 41)
    ]
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "spill", run_bytes_cap=2 * 1024, merge_fan_in=3, sort_key_column=KEY
    )
    try:
        for b in batches:
            sorter.write(b)
        assert len(list((tmp_path / "spill").glob("run_*.arrow"))) > 3
        sorter.finish()
        out = pa.Table.from_batches(list(sorter.iter_ordered()), schema=batches[0].schema)
        assert out.column(KEY).to_pylist() == values
    finally:
        sorter.close()


def test_exact_8_byte_rows_accepted_and_sorted(tmp_path):
    # The boundary is inclusive at exactly 8 bytes/row: a bare int64 key (8
    # bytes, no payload) has num_rows * 8 == nbytes, so the guard (`> nbytes`)
    # accepts it. It must sort correctly, proving the guard over-rejects
    # nothing valid at its exact boundary.
    n = 2_000
    values = list(range(n))
    order = list(range(n))
    random.Random(3).shuffle(order)
    shuffled = [values[i] for i in order]
    batches = [
        pa.record_batch({KEY: pa.array(shuffled[s : s + 41], type=pa.int64())})
        for s in range(0, n, 41)
    ]
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "spill", run_bytes_cap=2 * 1024, merge_fan_in=3, sort_key_column=KEY
    )
    try:
        for b in batches:
            sorter.write(b)
        # Force a real multi-pass merge so the tag-free split path is exercised
        # on exact-boundary rows, not just a single run.
        assert len(list((tmp_path / "spill").glob("run_*.arrow"))) > 3
        sorter.finish()
        out = pa.Table.from_batches(list(sorter.iter_ordered()), schema=batches[0].schema)
        assert out.column(KEY).to_pylist() == values
    finally:
        sorter.close()


def test_reject_key_type_drift(tmp_path):
    spill = tmp_path / "spill"
    sorter = BoundedExternalSorter(
        spill_dir=spill, run_bytes_cap=64 * 1024, merge_fan_in=4, sort_key_column=KEY
    )
    try:
        sorter.write(
            pa.record_batch(
                {KEY: pa.array([0, 1], type=pa.int64()), "payload": pa.array([b"a", b"b"])}
            )
        )
        with pytest.raises(ExecutionError) as exc:
            sorter.write(
                pa.record_batch(
                    {KEY: pa.array(["x", "y"], type=pa.string()), "payload": pa.array([b"c", b"d"])}
                )
            )
        assert exc.value.code == "out_of_core_sort_key_type_drift"
    finally:
        sorter.close()


# --- Failure cleanup registry ------------------------------------------------


def test_mid_finish_failure_leaks_no_run_file(tmp_path):
    # Fail the SECOND merge group after the first has succeeded: close() must
    # still remove the first group's intermediate (the registry hazard).
    spill = tmp_path / "spill"
    n = 1_500
    values = list(range(n))
    batches = _batches("int64", values, batch_size=31)
    sorter = BoundedExternalSorter(
        spill_dir=spill, run_bytes_cap=2 * 1024, merge_fan_in=2, sort_key_column=KEY
    )
    for b in batches:
        sorter.write(b)
    assert len(list(spill.glob("run_*.arrow"))) > 4  # multiple merge groups in pass 1

    original = sorter._merge_group
    calls = {"n": 0}

    def failing(group, output_path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected merge failure in the second group")
        return original(group, output_path)

    sorter._merge_group = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        sorter.finish()
    assert calls["n"] == 2  # the first group succeeded, the second failed
    sorter.close()
    assert list(spill.glob("*.arrow")) == []  # nothing leaked, including the first group's output


# --- Budget + state-guard error codes (the machine-consumed fields pinned) ----


def _int_batch(keys: list[int]) -> pa.RecordBatch:
    return pa.record_batch(
        {KEY: pa.array(keys, type=pa.int64()), "payload": pa.array([b"p"] * len(keys))}
    )


def test_reject_nonpositive_run_bytes_cap(tmp_path):
    with pytest.raises(ExecutionError) as exc:
        BoundedExternalSorter(spill_dir=tmp_path / "s", run_bytes_cap=0, merge_fan_in=4)
    assert exc.value.code == "out_of_core_reorder_budget_too_small"


def test_reject_merge_fan_in_below_two(tmp_path):
    with pytest.raises(ExecutionError) as exc:
        BoundedExternalSorter(spill_dir=tmp_path / "s", run_bytes_cap=1024, merge_fan_in=1)
    assert exc.value.code == "out_of_core_reorder_budget_too_small"


def test_minimal_viable_cap_and_fan_in_construct(tmp_path):
    # The smallest cap whose per-head cap (run_bytes_cap // (2 * fan_in)) does
    # not round to 0 must construct -- pins the `cap <= 0` and `fan_in < 2`
    # boundaries (a mutation to `cap <= 1` / `fan_in < 3` that rejects this valid
    # minimum reddens) AND the per-head-cap>=1 boundary just below.
    BoundedExternalSorter(spill_dir=tmp_path / "s", run_bytes_cap=4, merge_fan_in=2).close()


def test_reject_cap_too_small_for_per_head(tmp_path):
    # A positive cap whose per-head cap (run_bytes_cap // (2 * fan_in)) rounds to
    # 0 is rejected at construction rather than clamped to a useless 1-byte cap
    # that could never buffer a row. cap=3, fan_in=2 -> 3 // 4 == 0.
    with pytest.raises(ExecutionError) as exc:
        BoundedExternalSorter(spill_dir=tmp_path / "s", run_bytes_cap=3, merge_fan_in=2)
    assert exc.value.code == "out_of_core_reorder_budget_too_small"


def test_write_after_finish_raises(tmp_path):
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "s", run_bytes_cap=64 * 1024, merge_fan_in=4, sort_key_column=KEY
    )
    try:
        sorter.write(_int_batch([0, 1]))
        sorter.finish()
        with pytest.raises(ExecutionError) as exc:
            sorter.write(_int_batch([2, 3]))
        assert exc.value.code == "out_of_core_sort_invalid_state"
    finally:
        sorter.close()


def test_iter_ordered_before_finish_raises(tmp_path):
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "s", run_bytes_cap=64 * 1024, merge_fan_in=4, sort_key_column=KEY
    )
    try:
        sorter.write(_int_batch([0, 1]))
        with pytest.raises(ExecutionError) as exc:
            list(sorter.iter_ordered())
        assert exc.value.code == "out_of_core_sort_invalid_state"
    finally:
        sorter.close()


def test_single_row_batch_is_not_dropped(tmp_path):
    # Pins the empty-batch guard (`num_rows == 0`): a mutation to `== 1` would
    # silently drop a one-row batch.
    sorter = BoundedExternalSorter(
        spill_dir=tmp_path / "s", run_bytes_cap=64 * 1024, merge_fan_in=4, sort_key_column=KEY
    )
    try:
        sorter.write(_int_batch([5]))
        sorter.write(_int_batch([3]))
        sorter.finish()
        out = pa.Table.from_batches(list(sorter.iter_ordered()), schema=_int_batch([0]).schema)
        assert out.column(KEY).to_pylist() == [3, 5]
    finally:
        sorter.close()

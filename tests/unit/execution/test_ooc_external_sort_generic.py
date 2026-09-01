"""P4-A.2: the key-generalization of `BoundedExternalSorter`.

The ported `test_ooc_external_sort.py` proves the int-`row_nr` behavior + the
memory-cap envelope. This file proves the generalization: parity for every
allowlisted key type against an in-memory `sort_by` oracle, the string key
through a forced multi-pass merge across caps/fan-ins, partition-invariance,
the fail-closed key contract, and the failure-cleanup registry.
"""

from __future__ import annotations

import random

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
    if kind == "timestamp":
        return pa.array(values, type=pa.timestamp("us"))
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
    "timestamp",
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


@pytest.mark.parametrize("cap", [8 * 1024, 40 * 1024])
@pytest.mark.parametrize("fan_in", [2, 4])
def test_string_key_multipass_parity(cap, fan_in, tmp_path):
    # The generic multi-pass cutoff logic lives only in the merge; force
    # > merge_fan_in runs and compare full value+schema across caps x fan-ins.
    # n is large enough that even the biggest cap x fan-in makes > fan_in runs.
    n = 20_000
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


def test_duplicate_keys_sort_by_key_multiset_preserved(tmp_path):
    # Duplicate keys sort correctly BY KEY (non-decreasing) and lose no rows;
    # the tie order among equal keys is unspecified, so compare as a multiset.
    n = 4_000
    values = [i % 200 for i in range(n)]  # heavy duplication
    batches = _batches("int64", values, batch_size=53)
    out = _sorted(batches, tmp_path, cap=8 * 1024, fan_in=3)
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
    n = 5_000
    values = list(range(n))
    batches = _batches("string", values, batch_size=29)
    tiny = _sorted(batches, tmp_path, cap=4 * 1024, fan_in=2, name="tiny")
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
    n = 6_000
    values = list(range(n))
    batches = _batches("int64", values, batch_size=31)
    sorter = BoundedExternalSorter(
        spill_dir=spill, run_bytes_cap=8 * 1024, merge_fan_in=2, sort_key_column=KEY
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

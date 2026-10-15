"""D4: the reorder lane carries the masked payload as large_string/large_binary.

Plan: docs/plans/2026-10-15-d4-reorder-large-string-overflow.md. Arrow `string`
uses int32 offsets, so the external sorter's whole-run de-chunk overflows past
2^31 - 1 payload bytes. `run_ordered_join` widens the masked payload columns
before the sorter and the emit path narrows them back.

Tests here:
- wiring at the REAL `sorter.write` boundary (plus a negative control),
- byte-parity through real spills for a string and a binary payload,
- the opt-in forced-overflow reproducer (multi-GB, off by default).
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution.out_of_core import _stream_join as stream_join_mod
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._emit import assemble_resident
from decoy_engine.execution.out_of_core._external_sort import BoundedExternalSorter
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_from_tables
from decoy_engine.execution.out_of_core._stream_join import (
    ChildKeyLockstepCursor,
    JoinRowCursor,
    StreamFkJoiner,
)
from decoy_engine.execution.out_of_core._stream_join_cursors import _widen_payload_offsets
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

from ._ooc_fixtures import OocEdgeFixture, _plan, _seed
from ._ooc_reorder_harness import oracle_output

_N_PARENT = 60
_N_CHILD = 400
_BATCH_ROWS = 32
_RUN_BYTES_CAP = 4096
_MERGE_FAN_IN = 4


def _payload(kind: str) -> list[Any]:
    """Masked parent values covering null, empty, multi-byte unicode, embedded NUL,
    and (binary) non-UTF-8 bytes."""
    out: list[Any] = []
    for i in range(_N_PARENT):
        slot = i % 7
        if kind == "string":
            out.append(
                [None, "", "a\x00b", "é世🙂" + str(i), "plain" + str(i), "x" * (i + 1), " "][slot]
            )
        else:
            out.append(
                [None, b"", b"\xff\xfe\x00" + bytes([i]), b"\x00", bytes(range(i)), b"\x80", b"z"][
                    slot
                ]
            )
    return out


def _fixture(tmp_path: Path, kind: str) -> tuple[OocEdgeFixture, list[Any]]:
    """A PRESERVE edge whose masked parent payload is built directly (not through
    `mask_table`), so null/empty/NUL/non-UTF-8 values reach the sorter verbatim.

    Returns the fixture and the python-side expected FK output (the pandas-map
    equivalent: matched -> masked value, matched-null -> null, orphan -> raw key,
    null child -> null)."""
    key_type = pa.string() if kind == "string" else pa.binary()

    def enc(text: str | None) -> Any:
        return text if kind == "string" or text is None else text.encode()

    payload = _payload(kind)
    parent = pa.table({"key": pa.array([enc(f"p{i}") for i in range(_N_PARENT)], type=key_type)})
    masked_parent = pa.table({"key": pa.array(payload, type=key_type)})
    child_keys: list[str | None] = []
    expected: list[Any] = []
    for i in range(_N_CHILD):
        if i % 6 == 0:
            child_keys.append(None)
            expected.append(None)
        elif i % 3 == 0:
            child_keys.append(f"orphan{i}")
            expected.append(enc(f"orphan{i}"))
        else:
            child_keys.append(f"p{i % _N_PARENT}")
            expected.append(payload[i % _N_PARENT])
    edge = RelationshipEdge(
        parent_table="parents",
        parent_columns=("key",),
        child_table="children",
        child_columns=("key",),
        namespace="parent_rel",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    relation = build_parent_key_relation_from_tables(
        source_parent=parent,
        masked_parent=masked_parent,
        edge=edge,
        temp_dir=tmp_path / "relation",
    )
    child = pa.table(
        {
            "key": pa.array([enc(k) for k in child_keys], type=key_type),
            "amount": list(range(_N_CHILD)),
        }
    )
    fx = OocEdgeFixture(
        plan=_plan(_seed("passthrough"), parent="parents", child="children", column="key"),
        edge=edge,
        parent_relation=relation,
        child=child,
        child_key_types=(key_type,),
        remap_seeds=None,
        job_seed=None,
    )
    return fx, expected


class _SpySorter(BoundedExternalSorter):
    """The real sorter, recording every batch handed to `.write` (the boundary)."""

    instances: list[_SpySorter] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.written: list[pa.RecordBatch] = []
        type(self).instances.append(self)

    def write(self, batch: pa.RecordBatch) -> None:
        self.written.append(batch)
        super().write(batch)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> type[_SpySorter]:
    _SpySorter.instances = []
    monkeypatch.setattr(stream_join_mod, "BoundedExternalSorter", _SpySorter)
    return _SpySorter


def _drive(
    fx: OocEdgeFixture, tmp_path: Path, *, take_rows: int | None = None
) -> tuple[list[pa.Array], StreamFkJoiner]:
    """stage -> run_ordered_join -> cursor slices -> resolve_batch, in `take_rows`
    slices (so the cursor crosses sorted-batch boundaries)."""
    n = fx.child.num_rows
    step = take_rows or n
    resolved: list[pa.Array] = []
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches(max_chunksize=step))
        with joiner.run_ordered_join(
            _BATCH_ROWS, run_bytes_cap=_RUN_BYTES_CAP, merge_fan_in=_MERGE_FAN_IN
        ) as rows:
            cursor = JoinRowCursor(rows, join_columns=fx.edge.child_columns)
            child_cursor = ChildKeyLockstepCursor(joiner.open_child_key_reader())
            for start in range(0, n, step):
                count = min(step, n - start)
                slim = cursor.take(count, start)
                raw = child_cursor.take(count, start)
                resolved.append(joiner.resolve_batch(slim, raw)[0])
            cursor.assert_exhausted()
            child_cursor.assert_exhausted()
            child_cursor.close()
    return resolved, joiner


# ---------------------------------------------------------------------------
# Test 1 -- wiring at the real sorter.write boundary
# ---------------------------------------------------------------------------


def _assert_sorter_boundary(sorter: _SpySorter, expected_payload: pa.DataType) -> None:
    assert sorter.written, "the sorter received no batches"
    for batch in sorter.written:
        types = {f.name: f.type for f in batch.schema}
        assert set(types) == {
            "__decoy_row_nr",
            "__decoy_parent_match",
            "__decoy_parent_masked_0",
        }, f"slim column set drifted: {sorted(types)}"
        assert types["__decoy_row_nr"] == pa.int64()
        assert types["__decoy_parent_match"] == pa.bool_()
        assert types["__decoy_parent_masked_0"] == expected_payload


@pytest.mark.parametrize(
    ("kind", "wide"), [("string", pa.large_string()), ("binary", pa.large_binary())]
)
def test_sorter_boundary_receives_large_payload(tmp_path, spy, kind, wide) -> None:
    fx, _ = _fixture(tmp_path / "fx", kind)
    _drive(fx, tmp_path / "run")
    (sorter,) = spy.instances
    _assert_sorter_boundary(sorter, wide)


@pytest.mark.parametrize(("kind", "narrow"), [("string", pa.string()), ("binary", pa.binary())])
def test_boundary_assertion_fails_without_widening(
    tmp_path, spy, monkeypatch, kind, narrow
) -> None:
    # Negative control: bypass the widening call and the boundary assertion must
    # trip, proving the test locks the wiring and not only the helper.
    monkeypatch.setattr(stream_join_mod, "_widen_payload_offsets", lambda batch: batch)
    fx, _ = _fixture(tmp_path / "fx", kind)
    _drive(fx, tmp_path / "run")
    (sorter,) = spy.instances
    wide = pa.large_string() if kind == "string" else pa.large_binary()
    with pytest.raises(AssertionError):
        _assert_sorter_boundary(sorter, wide)
    _assert_sorter_boundary(sorter, narrow)  # and the unwidened schema is what arrived


def test_widen_leaves_non_payload_and_non_text_columns_alone() -> None:
    batch = pa.record_batch(
        {
            "__decoy_row_nr": pa.array([0, 1], type=pa.int64()),
            "__decoy_parent_match": pa.array([True, None], type=pa.bool_()),
            "__decoy_parent_masked_0": pa.array(["a", None], type=pa.string()),
            "__decoy_parent_masked_1": pa.array([1, 2], type=pa.int64()),
            "other": pa.array(["x", "y"], type=pa.string()),
        }
    )
    out = _widen_payload_offsets(batch)
    assert [f.type for f in out.schema] == [
        pa.int64(),
        pa.bool_(),
        pa.large_string(),
        pa.int64(),
        pa.string(),
    ]
    assert out.to_pydict() == batch.to_pydict()


# ---------------------------------------------------------------------------
# Test 2 -- byte-parity through real spills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["string", "binary"])
def test_reorder_payload_byte_parity_through_spills(tmp_path, spy, kind) -> None:
    fx, expected = _fixture(tmp_path / "fx", kind)
    final = pa.string() if kind == "string" else pa.binary()

    resolved, joiner = _drive(fx, tmp_path / "run", take_rows=70)  # 70 crosses sorted batches

    (sorter,) = spy.instances
    assert sorter._run_counter >= 2, "the run did not spill across >= 2 sorted runs"
    assert len(resolved) > 1

    # Final schema: the large type never leaks out of the resolved arrays.
    for arr in resolved:
        assert arr.type == final
    assert joiner.output_types == (final,)

    reorder = pa.concat_arrays(resolved)
    assert reorder.to_pylist() == expected  # python (pandas-map equivalent) oracle

    # Matched-null parent vs orphan: both present, and they resolve differently.
    child_keys = fx.child.column("key").to_pylist()
    matched_null = [i for i in range(_N_CHILD) if child_keys[i] is not None and expected[i] is None]
    orphans = [
        i
        for i in range(_N_CHILD)
        if child_keys[i] is not None and expected[i] is not None and expected[i] == child_keys[i]
    ]
    assert matched_null and orphans
    assert all(reorder[i].as_py() is None for i in matched_null)
    assert all(reorder[i].as_py() == child_keys[i] for i in orphans)

    # Executable single-edge oracle (mask_child_fk).
    oracle, _ = oracle_output(fx, tmp_path / "oracle")
    assert oracle.column("key").to_pylist() == expected
    assert reorder.to_pylist() == oracle.column("key").to_pylist()

    # Batch path (ChildFkBatchJoiner) over the same child.
    with ChildFkBatchJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "batch",
    ) as batch_joiner:
        batch_chunks = [
            batch_joiner.join_batch(b)[0] for b in fx.child.to_batches(max_chunksize=90)
        ]
    batch_key = pa.concat_arrays([b.column("key") for b in batch_chunks])
    assert batch_key.to_pylist() == reorder.to_pylist()
    assert batch_key.type == final

    # Resident output: whole-column type recovery keeps plain string/binary.
    batches = [
        pa.record_batch([arr, pa.array(range(len(arr)), type=pa.int64())], names=["key", "amount"])
        for arr in resolved
    ]
    resident = assemble_resident(batches, {"key": (joiner, 0)})
    assert resident.schema.field("key").type == final
    assert resident.column("key").to_pylist() == expected

    # Sink output: the fixed schema on disk is plain string/binary, values exact.
    sink_path = tmp_path / "sink.parquet"
    schema = pa.schema([pa.field("key", joiner.output_types[0])])
    with pq.ParquetWriter(sink_path, schema) as writer:
        for arr in resolved:
            writer.write_batch(pa.record_batch([arr], schema=schema))
    back = pq.read_table(sink_path)
    assert back.schema.field("key").type == final
    assert back.column("key").to_pylist() == expected


# ---------------------------------------------------------------------------
# Test 4 -- forced overflow reproducer (OPT-IN, multi-GB)
# ---------------------------------------------------------------------------

_INT32_MAX = 2**31 - 1


@pytest.mark.skipif(
    not os.environ.get("DECOY_RUN_HEAVY"),
    reason="multi-GB forced-overflow reproducer; set DECOY_RUN_HEAVY=1 to run",
)
def test_widening_lifts_the_int32_offset_overflow_in_one_run(tmp_path: Path) -> None:
    """One pre-sort run whose single masked payload column exceeds 2^31 - 1 bytes.

    OPT-IN: `DECOY_RUN_HEAVY=1 ~/bin/pytest-one <python> \\
    tests/unit/execution/test_ooc_reorder_large_string.py::\\
    test_widening_lifts_the_int32_offset_overflow_in_one_run`.
    Needs roughly 10 GB of free RAM and ~10 GB of tmp disk (the 2.2 GB payload, the
    flush's de-chunked copy, the sort `take` copy, the spilled run). The default
    devbox runs one test agent under a memory cap, so it stays out of the default suite.

    The run cap is deliberately LARGE (16 GiB) so every batch accumulates into ONE
    run; a low cap would only make smaller runs. Each fed batch is individually
    int32-representable (about 64 MiB). The same configuration must raise
    ArrowInvalid without the widening and complete with it.
    """
    rows_per_batch = 1_000_000
    value = "v" * 63 + "é"  # 65 UTF-8 bytes: multi-byte tail, uniform width
    value_bytes = len(value.encode())
    n_batches = _INT32_MAX // (rows_per_batch * value_bytes) + 2
    cap = 16 * 1024**3

    def payload_batches(start: int):
        payload = pa.array([value] * rows_per_batch, type=pa.string())
        for b in range(n_batches):
            # Reverse row numbers so the flush sort actually permutes the payload.
            top = (n_batches - b) * rows_per_batch + start
            row_nr = pa.array(range(top, top - rows_per_batch, -1), type=pa.int64())
            yield pa.record_batch(
                {
                    "__decoy_row_nr": row_nr,
                    "__decoy_parent_match": pa.array([True] * rows_per_batch, type=pa.bool_()),
                    "__decoy_parent_masked_0": payload,
                }
            )

    def run(widen: bool, name: str) -> int:
        sorter = BoundedExternalSorter(
            spill_dir=tmp_path / name,
            run_bytes_cap=cap,
            merge_fan_in=4,
            sort_key_column="__decoy_row_nr",
        )
        logical = 0
        try:
            for batch in payload_batches(0):
                # Logical bytes of the masked column, not table.nbytes (offsets/validity).
                logical += int(
                    pc.sum(pc.binary_length(batch.column("__decoy_parent_masked_0"))).as_py()
                )
                sorter.write(_widen_payload_offsets(batch) if widen else batch)
            assert logical > _INT32_MAX, "the single run does not exceed the int32 limit"
            sorter.finish()
            return sum(b.num_rows for b in sorter.iter_ordered())
        finally:
            sorter.close()
            gc.collect()

    with pytest.raises(pa.ArrowInvalid):
        run(widen=False, name="narrow")
    assert run(widen=True, name="wide") == n_batches * rows_per_batch

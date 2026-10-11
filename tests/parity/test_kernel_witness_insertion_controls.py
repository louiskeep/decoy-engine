"""Insertion-boundary controls for the native-execution witness (Phase D / D1a).

Driven through the real `_mask.mask_batch` so the spy sits at the actual binding.
Doubles stand in for a native backend; what is under test is that "native ran"
means the native RESULT is what the batch carries, not merely that a native call
returned.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.out_of_core import _mask
from decoy_engine.plan._types import SeedEnvelope, TableSeed
from tests.parity._kernel_witness import (
    ExpectedMask,
    KernelWitness,
    _same_buffers,
    _tokens,
)
from tests.parity.test_out_of_core_fk_parity import _seed

_EXPECTED = [ExpectedMask("t", "c", "redact_array")]
_SOURCES = {"t": pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})}


def _plan() -> Any:
    seed = TableSeed(per_column=(("c", _seed("redact")),), per_group=())
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=b"k" * 8, per_table=(("t", seed),)))


def _batch(values: list[str]) -> pa.RecordBatch:
    return pa.record_batch({"c": pa.array(values, type=pa.string())})


def _native_double(values: Any, **_: Any) -> pa.Array:
    return pa.array(["N"] * len(values), type=pa.string())


def _drive(
    witness: KernelWitness, batches: list[pa.RecordBatch], dispatch: dict[str, Callable[..., Any]]
) -> pa.Table:
    plan = _plan()
    with witness.observe(dispatch):
        outs = [_mask.mask_batch(plan, "t", b) for b in batches]
    return pa.Table.from_batches(outs)


def _wrap_inner(monkeypatch: pytest.MonkeyPatch, rewrite: Callable[[pa.RecordBatch], Any]) -> None:
    """Install a layer BELOW the witness (installed first, so the spy wraps it)."""
    real = _mask.mask_batch

    def inner(plan: Any, table_name: str, batch: Any, **kwargs: Any) -> Any:
        return rewrite(real(plan, table_name, batch, **kwargs))

    monkeypatch.setattr(_mask, "mask_batch", inner)


def test_native_result_that_the_batch_carries_is_consumed() -> None:
    w = KernelWitness()
    out = _drive(w, [_batch(["a", "b", "c"])], {"redact_array": w.native(_native_double)})
    w.assert_native_ran(_SOURCES, {"t": out}, _EXPECTED)


def test_discarded_native_result_with_an_equal_valued_python_array_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def python_equal(out: pa.RecordBatch) -> pa.RecordBatch:
        fresh = pa.array(out.column("c").to_pylist(), type=pa.string())
        return pa.record_batch([fresh], schema=out.schema)

    _wrap_inner(monkeypatch, python_equal)
    w = KernelWitness()
    out = _drive(w, [_batch(["a", "b", "c"])], {"redact_array": w.native(_native_double)})
    assert w.native_attempts == 1
    assert [o.backend for o in w.observations] == ["native"]
    # The value-only comparison the witness used to rely on cannot tell them apart...
    assert _tokens([w.observations[0].result]) == _tokens([out.column("c").combine_chunks()])
    # ...the insertion-boundary check can.
    with pytest.raises(AssertionError, match="discarded"):
        w.assert_native_ran(_SOURCES, {"t": out}, _EXPECTED)


def test_native_result_that_never_reaches_a_batch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _wrap_inner(monkeypatch, lambda out: pa.record_batch({"other": out.column("c")}))
    w = KernelWitness()
    out = _drive(w, [_batch(["a", "b", "c"])], {"redact_array": w.native(_native_double)})
    with pytest.raises(AssertionError, match="never inserted"):
        w.assert_native_ran(_SOURCES, {"t": out.rename_columns(["c"])}, _EXPECTED)


def test_observations_carry_table_column_and_batch_call_context() -> None:
    w = KernelWitness()
    sources = {"t": pa.table({"c": pa.array(["a", "b", "c", "d"], type=pa.string())})}
    out = _drive(
        w,
        [_batch(["a", "b"]), _batch(["c", "d"])],
        {"redact_array": w.native(_native_double)},
    )
    assert [(o.table, o.column, o.call) for o in w.observations] == [("t", "c", 0), ("t", "c", 1)]
    assert [(i.table, i.column, i.call) for i in w.insertions] == [("t", "c", 0), ("t", "c", 1)]
    w.assert_native_ran(sources, {"t": out}, _EXPECTED)


def test_same_buffers_distinguishes_equal_copies_from_the_same_array() -> None:
    a = pa.array(["x", "y"], type=pa.string())
    assert _same_buffers(a, a)
    assert not _same_buffers(a, pa.array(["x", "y"], type=pa.string()))
    empty = pa.array([], type=pa.string())
    assert _same_buffers(empty, pa.array([], type=pa.string()))

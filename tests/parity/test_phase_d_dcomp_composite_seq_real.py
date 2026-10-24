"""Phase D / Dcomp: the REAL compiled companion across the composite SEQ acceptance shapes.

Needs the abi-3 companion, where `test_phase_d_dcomp_composite_seq.py` carries the
companion-independent assertions with a stand-in kernel. Skipped only where NO companion is
installed; an installed companion that does not load (wrong ABI, failed self-test) FAILS every
case here instead (`_companion_gate`), so a stale or broken binary can never pass this suite by
skipping it. These tests run the real kernel and check what only it can show: native SEQ output
equals the full-frame oracle on every strict dimension, equals the SEQ Python path byte for
byte, is the kernel's own result rather than a recomputation, and does not move with the worker
thread count.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._sequential_native import SEQ_HASH_DISPOSITION_KEY
from decoy_engine.execution.native._crypto_ext import load_compiled_crypto_kernel
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dcomp_shapes as d
from tests.parity._companion_gate import CompanionState, require_companion
from tests.parity._d2a_seq_data import table_digest
from tests.parity._seq_witness import hide_companion, install

FAIL, WARN, PRESERVE, REMAP = (
    OrphanPolicy.FAIL,
    OrphanPolicy.WARN,
    OrphanPolicy.PRESERVE,
    OrphanPolicy.REMAP,
)


@pytest.fixture(autouse=True, scope="module")
def _companion() -> CompanionState:
    """Absent companion: skip. Installed but not loadable: fail, never skip."""
    return require_companion()


class RecordingRealKernel:
    """Delegates to the real compiled kernel, recording each array it returned and each thread grant."""

    def __init__(self, pin_threads: int | None = None) -> None:
        self._real = load_compiled_crypto_kernel()
        self._pin = pin_threads
        self.results: list[pa.Array] = []
        self.thread_grants: list[int | None] = []

    def derive_batch(self, values: Any, **kwargs: Any) -> pa.Array:
        if self._pin is not None:
            kwargs["native_threads"] = self._pin
        self.thread_grants.append(kwargs.get("native_threads"))
        out = self._real.derive_batch(values, **kwargs)
        self.results.append(out)
        return out


def _int_shape(policy: OrphanPolicy, n_type: pa.DataType, big: int) -> s.Shape:
    return d.int_component_shape(
        policy,
        parent_n=[big, 7, None],
        child_k=["a", "b", "q", None, "a", "c"],
        child_n=[big, 7, 5, 8, None, 9],
        n_type=n_type,
    )


_SHAPES: dict[str, Callable[[], s.Shape]] = {
    "composite-preserve": lambda: s.composite_shape(PRESERVE),
    "composite-remap": lambda: s.composite_shape(REMAP),
    "composite-warn": lambda: s.composite_shape(WARN),
    "golden-fail-no-orphan": lambda: d.golden_composite_shape(FAIL),
    "golden-warn-orphan": lambda: d.golden_composite_shape(WARN, orphan=True),
    "golden-remap-orphan": lambda: d.golden_composite_shape(REMAP, orphan=True),
    "golden-preserve-orphan": lambda: d.golden_composite_shape(PRESERVE, orphan=True),
    "int64-component-preserve": lambda: _int_shape(PRESERVE, pa.int64(), d.BIG),
    "int64-component-remap": lambda: _int_shape(REMAP, pa.int64(), d.BIG),
    "uint64-component-warn": lambda: _int_shape(WARN, pa.uint64(), d.UBIG),
    "empty-both": lambda: d.composite_tables(d.key_table([], []), d.key_table([], [])),
    "empty-parent-orphans": lambda: d.composite_tables(
        d.key_table([], []), d.key_table(["a", "q"], ["x", "w"])
    ),
    "all-null-child": lambda: d.composite_tables(
        d.key_table(["a", "b"], ["x", "y"]), d.key_table([None, None], [None, None])
    ),
}


@pytest.mark.parametrize("name", _SHAPES)
def test_native_seq_equals_the_oracle_and_the_python_route(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _SHAPES[name]()
    ff = s.run_full(shape)
    native = s.run_seq(shape)
    d.assert_strict_parity(ff, native, f"{name}: native vs ff")
    with monkeypatch.context() as m:
        hide_companion(m)
        python = s.run_seq(shape)
    for table in python.outputs:
        assert table_digest(native.outputs[table]) == table_digest(python.outputs[table]), (
            name,
            table,
        )
    # Warnings are their own channel: the orphan warning is identical on all three routes.
    orphan = [[w for w in r.warnings if w.code == "orphan_fk"] for r in (ff, native, python)]
    assert orphan[0] == orphan[1] == orphan[2], name
    backends = {
        e["executed_backend"]
        for e in native.quality_metrics[SEQ_HASH_DISPOSITION_KEY]
        if e["path"] == "scalar"
    }
    # A parent with rows runs on the companion; an empty parent computed nothing and says so.
    assert backends == ({"none"} if name.startswith("empty") else {"rust_companion"}), name


def test_fail_composite_orphan_raises_the_oracles_message_on_the_real_kernel() -> None:
    with pytest.raises(ExecutionError) as ff:
        s.run_full(d.golden_composite_shape(FAIL, orphan=True))
    with pytest.raises(ExecutionError) as seq:
        s.run_seq(d.golden_composite_shape(FAIL, orphan=True))
    assert ff.value.code == seq.value.code == "orphan_fk_violation"
    assert ff.value.message == seq.value.message


def test_composite_child_tuples_are_the_real_kernels_own_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, RecordingRealKernel())
    out = s.run_seq(s.composite_shape(REMAP)).outputs
    returned = {v for r in kernel.results for v in r.to_pylist()}
    p, c = out["P"].to_pydict(), out["C"].to_pydict()
    # Parent tokens and the minted orphan tokens are exactly what the kernel returned; the
    # matched child rows hold the parent's tokens and so were never recomputed.
    assert set(p["k"] + p["j"]) <= returned
    assert c["k"][2] in returned and c["j"][2] in returned
    assert (c["k"][0], c["j"][0]) == (p["k"][0], p["j"][0])


def _usable_cpus() -> int:
    getaffinity = getattr(os, "sched_getaffinity", None)
    return len(getaffinity(0)) if getaffinity is not None else (os.cpu_count() or 1)


@pytest.mark.skipif(_usable_cpus() < 2, reason="one usable core: every thread grant clamps to 1")
@pytest.mark.parametrize("name", ["composite-remap", "golden-remap-orphan"])
def test_composite_output_does_not_depend_on_the_worker_thread_count(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ff = s.run_full(_SHAPES[name]())
    digests: dict[int, dict[str, str]] = {}
    for threads in (1, 4, 8):
        with monkeypatch.context() as m:
            kernel = install(m, RecordingRealKernel(pin_threads=threads))
            result = s.run_seq(_SHAPES[name]())
        # The grant reached every kernel call, so the three runs really differ in thread count.
        assert kernel.thread_grants and set(kernel.thread_grants) == {threads}
        d.assert_strict_parity(ff, result, f"{name}: {threads} threads")
        digests[threads] = {t: table_digest(o) for t, o in result.outputs.items()}
    assert digests[1] == digests[4] == digests[8]

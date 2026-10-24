"""Phase D / Dcomp-OOC: the compiled companion on composite FK out-of-core (companion present).

The stand-in suites prove routing and consumption: the native-TAGGED result is the array the
batch carries. The tag there is applied by the harness around a Python stand-in, so it is not
proof that a compiled kernel executed. This file is that proof: the loaded kernel is the
compiled wrapper at the ABI the engine requires, the route's evidence names `rust_companion`,
and the kernel's tokens on composite parent key components equal the oracle's and the
companion-hidden Python route's byte for byte, on every run mode, at 1 and 4 native threads
and at every batch size the resident suite pins, with the lane each mode must build asserted.
Skipped only where NO companion is installed; an installed companion that does not load (wrong
ABI, failed self-test) FAILS every case here instead (`_companion_gate`), so a stale binary can
never pass this suite by skipping it.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution.native import _crypto_ext
from decoy_engine.execution.native._crypto_ext import load_compiled_crypto_kernel
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dcomp_ooc_shapes as o
from tests.parity import _dcomp_shapes as d
from tests.parity._companion_gate import CompanionState, require_companion
from tests.parity._d1a_hash_data import PythonReferenceKernel
from tests.parity._kernel_witness import force_companion_unavailable

P, R, W = OrphanPolicy.PRESERVE, OrphanPolicy.REMAP, OrphanPolicy.WARN
ENGINE_ABI = "decoy-native-abi-3"
BATCH_SIZES = [1, 2, 3, None]
BATCH_IDS = ["rows1", "rows2", "rows3", "default"]
THREADS = [1, 4]


@pytest.fixture(autouse=True, scope="module")
def _companion() -> CompanionState:
    """Absent companion: skip. Installed but not loadable: fail, never skip."""
    return require_companion()


_SHAPES = {
    "composite-remap": lambda: s.composite_shape(R),
    "composite-preserve": lambda: s.composite_shape(P),
    "int-component-remap": lambda: d.int_component_shape(
        R, parent_n=[1, None, 3], child_k=["a", "c", "q", "a"], child_n=[1, 3, 9, None]
    ),
    "golden-warn": lambda: d.golden_composite_shape(W, orphan=True),
}


@functools.cache
def _oracle(name: str) -> Any:
    """The oracle does not see the run mode, thread count or batch size: one run per shape."""
    return s.run_full(_SHAPES[name]())


def test_the_loaded_companion_is_the_compiled_kernel_at_the_engine_abi() -> None:
    """The evidence below is proof of the compiled kernel only if the loaded object IS the
    compiled wrapper (never the stand-in) and the binary reports the ABI the engine requires."""
    from decoy_engine_native import _kernel

    kernel = load_compiled_crypto_kernel()
    assert type(kernel).__name__ == "_CompiledKeyedDerivationKernel"
    assert not isinstance(kernel, PythonReferenceKernel)
    assert _kernel.abi_version() == _crypto_ext._EXPECTED_ABI_VERSION == ENGINE_ABI


@pytest.mark.parametrize("batch_rows", BATCH_SIZES, ids=BATCH_IDS)
@pytest.mark.parametrize("native_threads", THREADS, ids=["threads1", "threads4"])
@pytest.mark.parametrize("mode", o.MODES)
@pytest.mark.parametrize("name", sorted(_SHAPES))
def test_real_kernel_equals_the_oracle_and_the_python_route(
    name: str,
    mode: str,
    native_threads: int,
    batch_rows: int | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _SHAPES[name]()
    kw: dict[str, Any] = {"batch_rows": batch_rows, "native_threads": native_threads}
    native = o.run_ooc(shape, mode, tmp_path=tmp_path / "native", **kw)
    o.assert_lane(native, mode)
    backends = {e["executed_backend"] for e in o.hash_evidence(native) if e["path"] == "mask_batch"}
    assert backends == {"rust_companion"}
    oracle = _oracle(name)
    o.assert_agrees(oracle, native)
    assert o.sorted_warnings(native.warnings) == o.sorted_warnings(oracle.warnings)
    force_companion_unavailable(monkeypatch)
    python = o.run_ooc(shape, mode, tmp_path=tmp_path / "python", **kw)
    o.assert_lane(python, mode)
    for table in shape.sources:
        assert native.outputs[table].equals(python.outputs[table], check_metadata=True), table
    assert o.sorted_warnings(python.warnings) == o.sorted_warnings(native.warnings)

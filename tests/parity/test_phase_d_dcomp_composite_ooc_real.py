"""Phase D / Dcomp-OOC: the compiled companion on composite FK out-of-core (companion present).

The stand-in suites prove routing; this one proves the real kernel's tokens on composite
parent key components are the Python tokens byte for byte, on all three run modes, so the
strict agreement with the oracle holds with the shipped kernel and not only with a double.
Skipped where the companion is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dcomp_ooc_shapes as o
from tests.parity import _dcomp_shapes as d
from tests.parity._kernel_witness import force_companion_unavailable

P, R, W = OrphanPolicy.PRESERVE, OrphanPolicy.REMAP, OrphanPolicy.WARN


def _companion_loads() -> bool:
    try:
        load_compiled_crypto_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _companion_loads(), reason="compiled companion not installed")

_SHAPES = {
    "composite-remap": lambda: s.composite_shape(R),
    "composite-preserve": lambda: s.composite_shape(P),
    "int-component-remap": lambda: d.int_component_shape(
        R, parent_n=[1, None, 3], child_k=["a", "c", "q", "a"], child_n=[1, 3, 9, None]
    ),
    "golden-warn": lambda: d.golden_composite_shape(W, orphan=True),
}


@pytest.mark.parametrize("mode", o.MODES)
@pytest.mark.parametrize("name", sorted(_SHAPES))
def test_real_kernel_equals_the_oracle_and_the_python_route(
    name: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _SHAPES[name]()
    native = o.run_ooc(shape, mode, tmp_path=tmp_path / "native", batch_rows=2)
    backends = {e["executed_backend"] for e in o.hash_evidence(native) if e["path"] == "mask_batch"}
    assert backends == {"rust_companion"}
    oracle = s.run_full(shape)
    assert o.strict(oracle, native) == {t: o.METADATA_ONLY for t in oracle.outputs}
    assert o.sorted_warnings(native.warnings) == o.sorted_warnings(oracle.warnings)
    force_companion_unavailable(monkeypatch)
    python = o.run_ooc(shape, mode, tmp_path=tmp_path / "python", batch_rows=2)
    for table in shape.sources:
        assert native.outputs[table].equals(python.outputs[table], check_metadata=True), table

"""Phase D / Dcomp-OOC: which kernel produced each composite key column out-of-core.

Composite parent key components are ordinary hash columns to `mask_batch`, so the compiled
kernel's result must be the very array the emitted batch carries (the insertion-boundary
witness, `_kernel_witness.py`). The child group is resolved by the join, never by a kernel:
no observation is attributed to a child column. Under REMAP the orphan tuple is minted with
the Python hash (the D1a `fk_key_hash_remap_native_deferred` disposition): that call is
observed as legacy, outside `mask_batch`, over the child key values, and is pinned here as
Python so the evidence and the witness agree. The companion stand-in runs on every box; the
real kernel is `test_phase_d_dcomp_composite_ooc_real.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.out_of_core import _mask, _runner, _stream_driver
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dcomp_ooc_shapes as o
from tests.parity._d1a_hash_data import install_companion
from tests.parity._kernel_witness import (
    LEGACY,
    ExpectedMask,
    KernelWitness,
    force_companion_unavailable,
)
from tests.parity._strict_compare import _cell_token

P, R = OrphanPolicy.PRESERVE, OrphanPolicy.REMAP
_PARENTS = [ExpectedMask("P", "k", "hash_array"), ExpectedMask("P", "j", "hash_array")]


def _run(shape: s.Shape, mode: str, tmp_path: Path) -> tuple[KernelWitness, o.OocRun]:
    witness = KernelWitness()
    with witness.observe():
        run = o.run_ooc(shape, mode, tmp_path=tmp_path, batch_rows=2)
    return witness, run


@pytest.mark.parametrize("mode", o.MODES)
def test_composite_parent_key_components_are_native_consumed_and_the_child_is_join_resolved(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    shape = s.composite_shape(P)
    witness, run = _run(shape, mode, tmp_path)
    witness.assert_covered(shape.sources, _PARENTS)
    # Sink outputs are read back from parquet, so the consumed check is on the resident path;
    # the insertion check inside `assert_native_ran` holds on every mode.
    witness.assert_native_ran(shape.sources, run.outputs, _PARENTS)
    assert not any(obs.table == "C" for obs in witness.observations)
    assert not any(obs.table is None for obs in witness.observations)  # PRESERVE mints nothing


@pytest.mark.parametrize("mode", o.MODES)
def test_remap_mints_the_orphan_tuple_with_the_python_hash_outside_mask_batch(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    shape = s.composite_shape(R)
    witness, run = _run(shape, mode, tmp_path)
    mints = [obs for obs in witness.observations if obs.table is None]
    assert mints and {obs.backend for obs in mints} == {LEGACY}
    assert {obs.logical for obs in mints} == {"hash_array"}
    # Every mint call hashes child key values (per batch, per component), never parent values.
    child_tokens = {
        _cell_token(v) for c in ("k", "j") for v in shape.sources["C"].column(c).to_pylist()
    }
    assert all(_cell_token(v) in child_tokens for obs in mints for v in obs.values.to_pylist())
    # The parent key components themselves are still native (the mints are attributed by their
    # values, so the parent assertion excludes them explicitly).
    for expected in _PARENTS:
        parent = [
            obs
            for obs in witness.observations
            if (obs.table, obs.column) == (expected.table, expected.column)
        ]
        assert parent and all(obs.backend == "native" for obs in parent)


@pytest.mark.parametrize("mode", o.MODES)
def test_without_the_companion_the_parent_keys_run_python_only(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_companion_unavailable(monkeypatch)
    shape = s.composite_shape(P)
    witness, _ = _run(shape, mode, tmp_path)
    witness.assert_legacy_path_only(shape.sources, _PARENTS)


def test_a_discarded_native_parent_key_fails_the_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    real = _mask.mask_batch

    def python_equal(*args: Any, **kwargs: Any) -> pa.RecordBatch:
        out = real(*args, **kwargs)
        idx = out.schema.get_field_index("k")
        if idx < 0 or args[1] != "P":
            return out
        cols = list(out.columns)
        cols[idx] = pa.array(cols[idx].to_pylist(), pa.string())
        return pa.record_batch(cols, schema=out.schema)

    for module in (_mask, _runner, _stream_driver):
        monkeypatch.setattr(module, "mask_batch", python_equal)
    shape = s.composite_shape(P)
    witness, run = _run(shape, "resident", tmp_path)
    assert witness.native_attempts > 0
    assert (
        run.outputs["P"].column("k").to_pylist()
        == o.run_ooc(shape).outputs["P"].column("k").to_pylist()
    )
    with pytest.raises(AssertionError, match="discarded"):
        witness.assert_native_ran(shape.sources, run.outputs, _PARENTS)

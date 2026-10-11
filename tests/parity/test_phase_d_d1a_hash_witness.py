"""Phase D / D1a: the witness proves the native hash result is CONSUMED, on the real route.

The reference double stands in for the companion so these hold on any box; the real
kernel's byte parity is covered by the companion-present suite. What is asserted here is
routing: a native hash result is the array the batch carries (both drivers, both output
paths), a discarded one fails the witness, a parent-key column is masked once through
`mask_batch` and never again by the relation build, and the D0 legacy-only baseline
flips to native exactly for the cells D1a claims.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.out_of_core import _mask, _runner, _stream_driver
from tests.parity._d1a_hash_data import chain_sources, hash_seeds, install_companion, run_chain
from tests.parity._kernel_witness import ExpectedMask, KernelWitness, force_companion_unavailable
from tests.parity.test_out_of_core_fk_parity import _run_ooc
from tests.parity.test_phase_d_d0_baseline import _SUCCESS_CASES, Case
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

_PAYLOAD = pa.table(
    {
        "a": pa.array(["w", "x", None, "z", "q"], pa.string()),
        "n": pa.array([1, 2, 3, None, 5], pa.int64()),
    }
)
_SEEDS = hash_seeds(["a", "n"])
_EXPECTED = [ExpectedMask("t", c, "hash_array") for c in ("a", "n", "hk")]
_MODES = ["resident", "sink-batch", "sink-reorder"]


@pytest.mark.parametrize("mode", _MODES)
def test_native_hash_is_consumed_at_the_insertion_boundary(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(_PAYLOAD, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    sources = chain_sources(_PAYLOAD)
    witness.assert_covered(sources, _EXPECTED)
    witness.assert_native_ran(sources, run.tables, _EXPECTED)
    with pytest.raises(AssertionError):
        witness.assert_legacy_path_only(sources, _EXPECTED)


@pytest.mark.parametrize("mode", _MODES)
def test_declined_hash_is_witnessed_python_only(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_companion_unavailable(monkeypatch)
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(_PAYLOAD, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    sources = chain_sources(_PAYLOAD)
    witness.assert_legacy_path_only(sources, _EXPECTED)
    with pytest.raises(AssertionError):
        witness.assert_native_ran(sources, run.tables, _EXPECTED)


@pytest.mark.parametrize("mode", _MODES)
def test_a_discarded_native_result_fails_the_witness_on_the_real_route(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    real = _mask.mask_batch

    def python_equal(*args: Any, **kwargs: Any) -> pa.RecordBatch:
        # Below the witness: the native kernel ran and was inserted, then an equal-valued
        # Python array replaces it before the batch leaves mask_batch.
        out = real(*args, **kwargs)
        idx = out.schema.get_field_index("a")
        if idx < 0:
            return out
        cols = list(out.columns)
        cols[idx] = pa.array(cols[idx].to_pylist(), pa.string())
        return pa.record_batch(cols, schema=out.schema)

    for module in (_mask, _runner, _stream_driver):
        monkeypatch.setattr(module, "mask_batch", python_equal)
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(_PAYLOAD, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    sources = chain_sources(_PAYLOAD)
    assert witness.native_attempts > 0
    assert (
        run.tables["t"].column("a").to_pylist()
        == run_chain(_PAYLOAD, _SEEDS).tables["t"].column("a").to_pylist()
    )
    with pytest.raises(AssertionError, match="discarded"):
        witness.assert_native_ran(sources, run.tables, [ExpectedMask("t", "a", "hash_array")])


@pytest.mark.parametrize("mode", _MODES)
def test_parent_key_is_masked_once_per_batch_and_the_relation_copy_hashes_nothing(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    witness = KernelWitness()
    with witness.observe():
        run_chain(_PAYLOAD, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    hk_calls = [o for o in witness.observations if (o.table, o.column) == ("t", "hk")]
    assert len(hk_calls) == 3  # 5 rows at 2 per batch: one native call per batch
    assert {o.backend for o in hk_calls} == {"native"}
    # Nothing hashes outside mask_batch: the aligned relation copy is not a hash invocation.
    assert [o for o in witness.observations if o.table is None and o.logical == "hash_array"] == []


@pytest.mark.parametrize(
    "case", [c for c in _SUCCESS_CASES if c.strategy == "hash"], ids=lambda c: c.id
)
def test_d0_hash_cells_flip_from_legacy_to_native(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    plan, sources, graph = case.build()
    witness = KernelWitness()
    with witness.observe():
        ooc = _run_ooc(plan, sources, graph).outputs
    witness.assert_covered(sources, case.masked)
    witness.assert_native_ran(sources, ooc, case.masked)
    with pytest.raises(AssertionError):
        witness.assert_legacy_path_only(sources, case.masked)

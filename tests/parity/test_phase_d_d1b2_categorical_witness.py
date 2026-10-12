"""Phase D / D1b-ii: the witness proves the native categorical result is CONSUMED.

The reference double stands in for the index kernel so these hold on any box; the real
kernel's byte parity is covered by the companion-present suite. What is asserted here is
routing, observed at the categorical binding (the Group (b) adapter, with native provenance
taken from the compiled operator it calls): a native result is the array the batch carries
(both drivers, both output paths), a discarded one fails the witness even when an
equal-valued Python array replaces it, an adapter call that made no kernel call never reads
as a native execution, and a declined column is witnessed as Python only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.out_of_core import _hash_run, _mask, _runner, _stream_driver
from tests.parity._d1a_hash_data import chain_sources, run_chain
from tests.parity._d1b2_categorical_data import (
    categorical_seed,
    install_index_companion,
)
from tests.parity._kernel_witness import (
    CATEGORICAL_ADAPTER,
    IDLE,
    NATIVE,
    UNKNOWN,
    ExpectedMask,
    KernelWitness,
    force_companion_unavailable,
)
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

_PAYLOAD = pa.table({"a": pa.array(["w", "x", None, "z", "q"], pa.string())})
_SEEDS = (("a", categorical_seed()),)
_EXPECTED = [ExpectedMask("t", "a", "categorical_array")]
_MODES = ["resident", "sink-batch", "sink-reorder"]


def hide_hash_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the chain's hash parent key on Python so the witness counts categorical calls only."""

    def unavailable() -> None:
        raise CryptoExtensionUnavailableError("hash companion hidden to isolate the count")

    monkeypatch.setattr(_hash_run, "load_compiled_crypto_kernel", unavailable)


@pytest.mark.parametrize("mode", _MODES)
def test_native_categorical_is_consumed_at_the_insertion_boundary(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(_PAYLOAD, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    sources = chain_sources(_PAYLOAD)
    witness.assert_covered(sources, _EXPECTED)
    witness.assert_native_ran(sources, run.tables, _EXPECTED)
    # The witness counted the compiled operator's own calls: one per batch, the same number
    # the kernel saw. A call count alone proves nothing, so the buffer identity above is the claim.
    assert witness.native_attempts == state["kernel"].calls == 3
    with pytest.raises(AssertionError):
        witness.assert_legacy_path_only(sources, _EXPECTED)


@pytest.mark.parametrize("mode", _MODES)
def test_declined_categorical_is_witnessed_python_only(
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
    install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    real = _mask.mask_batch

    def python_equal(*args: Any, **kwargs: Any) -> pa.RecordBatch:
        # Below the witness: the native operator ran and was inserted, then an equal-valued
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
    plain = run_chain(_PAYLOAD, _SEEDS)
    assert run.tables["t"].column("a").to_pylist() == plain.tables["t"].column("a").to_pylist()
    with pytest.raises(AssertionError, match="discarded"):
        witness.assert_native_ran(sources, run.tables, _EXPECTED)


@pytest.mark.parametrize("mode", _MODES)
def test_an_all_null_column_is_idle_not_a_native_execution(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    payload = pa.table({"a": pa.array([None, None, None], pa.string())})
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    sources = chain_sources(payload)
    assert state["kernel"].calls == witness.native_attempts == 0
    backends = {o.backend for o in witness.observations if (o.table, o.column) == ("t", "a")}
    assert backends == {IDLE}
    with pytest.raises(AssertionError, match="want native"):
        witness.assert_native_ran(sources, run.tables, _EXPECTED)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "values",
    [["x", "y", None, None], [None, None, "x", "y"]],
    ids=["populated-then-null", "null-then-populated"],
)
def test_an_idle_batch_does_not_overwrite_a_native_execution(
    values: list[str | None], mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    payload = pa.table({"a": pa.array(values, pa.string())})
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    backends = {o.backend for o in witness.observations if (o.table, o.column) == ("t", "a")}
    assert backends == {NATIVE, IDLE}
    witness.assert_native_ran(chain_sources(payload), run.tables, _EXPECTED)
    assert witness.native_attempts == 1


@pytest.mark.parametrize("mode", _MODES)
def test_a_populated_python_batch_cannot_launder_as_idle(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex final-gate HIGH: a mixed column (one native batch + one POPULATED batch answered
    # with no native call) must NOT pass as native-consumed. IDLE is the verified empty/all-null
    # short-circuit only; a populated no-native result is UNKNOWN and fails the whole-column proof.
    install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    payload = pa.table({"a": pa.array(["w", "x", "y", "z"], pa.string())})
    real_adapter = _mask.native_categorical_array
    seen = {"populated": 0}

    def laundering_adapter(values: Any, *args: Any, **kwargs: Any) -> pa.Array:
        col = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
        populated = len(col) > 0 and col.null_count < len(col)
        if populated:
            seen["populated"] += 1
            if seen["populated"] >= 2:
                # A populated batch answered WITHOUT a native call (the laundering path).
                return pa.array(["w"] * len(col), pa.string())
        return real_adapter(values, *args, **kwargs)

    witness = KernelWitness()
    with witness.observe(dispatch={CATEGORICAL_ADAPTER: laundering_adapter}):
        run = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    sources = chain_sources(payload)
    backends = {o.backend for o in witness.observations if (o.table, o.column) == ("t", "a")}
    assert NATIVE in backends and UNKNOWN in backends and IDLE not in backends
    with pytest.raises(AssertionError, match="want native"):
        witness.assert_native_ran(sources, run.tables, _EXPECTED)


@pytest.mark.parametrize("mode", _MODES)
def test_parent_key_hash_and_categorical_payload_are_witnessed_independently(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    witness = KernelWitness()
    with witness.observe():
        run_chain(_PAYLOAD, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    categorical = [o for o in witness.observations if o.logical == "categorical_array"]
    assert {(o.table, o.column) for o in categorical} == {("t", "a")}
    assert len(categorical) == 3

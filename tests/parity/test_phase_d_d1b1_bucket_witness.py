"""Phase D / D1b-i: the witness proves the native bucket_perturb result is CONSUMED.

The reference double stands in for the index kernel so these hold on any box; the real kernel's
byte parity is covered by the companion-present suite. What is asserted here is routing,
observed at the bucket_perturb binding (the Group (c) seam's adapter, with native provenance
taken from the compiled operator's own `derive_calls`): a native result is the array the batch
carries (both drivers, both output paths), a discarded one fails the witness even when an
equal-valued Python array replaces it, a zero-compiled-call batch counts as idle only when it is
independently verified empty, all-null or all-unparseable with output equal to input, and a
declined column is witnessed as Python only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.out_of_core import (
    _hash_run,
    _mask,
    _mask_group_c,
    _runner,
    _stream_driver,
)
from decoy_engine.transforms.bucket_perturb import apply_bucket_perturb
from tests.parity._d1a_hash_data import chain_sources, run_chain
from tests.parity._d1b1_bucket_data import bucket_seed, install_index_companion
from tests.parity._kernel_witness import (
    BUCKET_ADAPTER,
    IDLE,
    NATIVE,
    UNKNOWN,
    ExpectedMask,
    KernelWitness,
    bucket_verified_idle,
    force_companion_unavailable,
)
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

_PAYLOAD = pa.table(
    {"a": pa.array(["2024-03-05", "2023-07-19", None, "2022-11-30", "2021-02-14"], pa.string())}
)
_SEEDS = (("a", bucket_seed("week")),)
_EXPECTED = [ExpectedMask("t", "a", "bucket_perturb_array")]
_MODES = ["resident", "sink-batch", "sink-reorder"]
_UNPARSEABLE = ["nope", "bad", "2024-99-99", ""]


def hide_hash_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the chain's hash parent key on Python so the witness counts bucket calls only."""

    def unavailable() -> None:
        raise CryptoExtensionUnavailableError("hash companion hidden to isolate the count")

    monkeypatch.setattr(_hash_run, "load_compiled_crypto_kernel", unavailable)


def backends_of(witness: KernelWitness) -> set[str]:
    return {o.backend for o in witness.observations if (o.table, o.column) == ("t", "a")}


@pytest.mark.parametrize("mode", _MODES)
def test_native_bucket_perturb_is_consumed_at_the_insertion_boundary(
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
    # The witness counted the compiled operator's own calls: one per batch holding a date (a
    # week bucket has one size), the same number the kernel saw.
    assert witness.native_attempts == state["kernel"].calls == 3
    with pytest.raises(AssertionError):
        witness.assert_legacy_path_only(sources, _EXPECTED)


@pytest.mark.parametrize("mode", _MODES)
def test_declined_bucket_perturb_is_witnessed_python_only(
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
        cols[idx] = pa.array(cols[idx].to_pylist(), cols[idx].type)
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
@pytest.mark.parametrize(
    "values",
    [[None, None, None], _UNPARSEABLE[:3]],
    ids=["all-null", "all-unparseable"],
)
def test_a_column_with_nothing_to_perturb_is_idle_not_a_native_execution(
    values: list[str | None], mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    payload = pa.table({"a": pa.array(values, pa.string())})
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    assert state["kernel"].calls == witness.native_attempts == 0
    assert backends_of(witness) == {IDLE}
    with pytest.raises(AssertionError, match="want native"):
        witness.assert_native_ran(chain_sources(payload), run.tables, _EXPECTED)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "values",
    [
        ["2024-03-05", "2024-03-06", None, None],
        [None, None, "2024-03-05", "2024-03-06"],
        ["2024-03-05", "2024-03-06", "nope", "bad"],
        ["nope", "bad", "2024-03-05", "2024-03-06"],
    ],
    ids=[
        "populated-then-null",
        "null-then-populated",
        "populated-then-unparseable",
        "unparseable-then-populated",
    ],
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
    assert backends_of(witness) == {NATIVE, IDLE}
    witness.assert_native_ran(chain_sources(payload), run.tables, _EXPECTED)
    assert witness.native_attempts == 1


def _python_equal_output(
    values: Any, *, mask_key: bytes, namespace: str, bucket: str, date_format: str
) -> pa.Array:
    """The reference output for a slice, computed without the compiled operator."""
    perturbed = apply_bucket_perturb(
        values.to_pandas(),
        bucket=bucket,
        job_seed=mask_key,
        namespace=namespace,
        date_format=date_format,
    )
    return pa.array(perturbed, from_pandas=True)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("laundered", [1, 2], ids=["laundered-first", "laundered-second"])
def test_a_populated_parseable_python_batch_cannot_launder_as_idle(
    laundered: int, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mixed column (one native batch + one POPULATED, PARSEABLE batch answered by Python with
    # no compiled call) must not pass as native-consumed, whichever order the batches arrive in.
    # `derive_calls == 0` alone proves native did not run, not that it was unnecessary.
    install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    payload = pa.table({"a": pa.array(["2024-03-05", "2024-03-06", "2023-07-19", "2023-07-20"])})
    real_adapter = _mask_group_c.native_bucket_perturb_array
    seen = {"populated": 0}

    def laundering_adapter(values: Any, **kwargs: Any) -> pa.Array:
        col = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
        if len(col) > 0 and col.null_count < len(col):
            seen["populated"] += 1
            if seen["populated"] == laundered:
                return _python_equal_output(
                    col,
                    mask_key=kwargs["mask_key"],
                    namespace=kwargs["namespace"],
                    bucket=kwargs["bucket"],
                    date_format=kwargs["date_format"],
                )
        return real_adapter(values, **kwargs)

    witness = KernelWitness()
    with witness.observe(dispatch={BUCKET_ADAPTER: laundering_adapter}):
        run = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    backends = backends_of(witness)
    assert NATIVE in backends and UNKNOWN in backends and IDLE not in backends
    with pytest.raises(AssertionError, match="want native"):
        witness.assert_native_ran(chain_sources(payload), run.tables, _EXPECTED)


def test_an_unparseable_batch_is_idle_only_when_the_output_equals_the_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    present = pa.array(["nope", "bad"], pa.string())
    assert bucket_verified_idle(present, present, "%Y-%m-%d")
    assert not bucket_verified_idle(present, pa.array(["NOPE", "bad"], pa.string()), "%Y-%m-%d")
    assert not bucket_verified_idle(present, present, None)
    # A parseable value is never idle, even when the output happens to equal the input.
    parseable = pa.array(["2024-03-05", "bad"], pa.string())
    assert not bucket_verified_idle(parseable, parseable, "%Y-%m-%d")
    assert bucket_verified_idle(pa.array([], pa.string()), pa.nulls(0), "%Y-%m-%d")
    assert bucket_verified_idle(pa.nulls(3), pa.nulls(3), "%Y-%m-%d")
    assert not bucket_verified_idle(pa.nulls(3), pa.array(["x", None, None]), "%Y-%m-%d")


@pytest.mark.parametrize("mode", _MODES)
def test_an_unparseable_batch_with_altered_output_is_not_idle(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    payload = pa.table({"a": pa.array(["2024-03-05", "2024-03-06", "nope", "bad"], pa.string())})
    real_adapter = _mask_group_c.native_bucket_perturb_array

    def altering_adapter(values: Any, **kwargs: Any) -> pa.Array:
        out = real_adapter(values, **kwargs)
        if "nope" in values.to_pylist():
            return pa.array(["changed", "bad"], pa.string())
        return out

    witness = KernelWitness()
    with witness.observe(dispatch={BUCKET_ADAPTER: altering_adapter}):
        run = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    assert UNKNOWN in backends_of(witness)
    with pytest.raises(AssertionError, match="want native"):
        witness.assert_native_ran(chain_sources(payload), run.tables, _EXPECTED)


@pytest.mark.parametrize("mode", _MODES)
def test_parent_key_hash_and_bucket_payload_are_witnessed_independently(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    hide_hash_companion(monkeypatch)
    witness = KernelWitness()
    with witness.observe():
        run_chain(_PAYLOAD, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    bucket = [o for o in witness.observations if o.logical == "bucket_perturb_array"]
    assert {(o.table, o.column) for o in bucket} == {("t", "a")}
    assert len(bucket) == 3


def test_the_witness_spies_the_bucket_binding_not_hash_categorical_or_scalar() -> None:
    witness = KernelWitness()
    with witness.observe():
        assert _mask_group_c.native_bucket_perturb_array is not None
        assert _mask_group_c.native_bucket_perturb_array.__name__ == "spy"

"""Provenance controls for the native-execution witness.

Test doubles stand in for a future native+fallback dispatch and are driven
through the real `_mask.mask_column`, so the spy is exercised at the actual
lookup binding. Only a native-only success may satisfy `assert_native_ran`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.out_of_core import _mask
from tests.parity._kernel_witness import ENTRYPOINTS, ExpectedMask, KernelWitness
from tests.parity.test_out_of_core_fk_parity import _seed

_EXPECTED = [ExpectedMask("t", "c", "redact_array")]
_SOURCES = {"t": pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})}


class _NativeDeclinedError(Exception):
    pass


def _native_double(values: Any, **_: Any) -> pa.Array:
    return pa.array(["N"] * len(values), type=pa.string())


def _native_raises(values: Any, **_: Any) -> pa.Array:
    raise _NativeDeclinedError


def _drive(witness: KernelWitness, dispatch: dict[str, Callable[..., Any]] | None) -> pa.Table:
    """Run the real mask_column for the fixture's one redact column under the spy."""
    with witness.observe(dispatch):
        out = _mask.mask_column(_SOURCES["t"].column("c"), _seed("redact"), b"k" * 8, column="c")
    return pa.table({"c": out})


def _assert_fails(call: Callable[[], None]) -> None:
    with pytest.raises(AssertionError):
        call()


def test_scenario_1_native_only_success() -> None:
    w = KernelWitness()
    outputs = _drive(w, {"redact_array": w.native(_native_double)})
    assert outputs.column("c").to_pylist() == ["N", "N", "N"]
    w.assert_covered(_SOURCES, _EXPECTED)
    w.assert_native_ran(_SOURCES, {"t": outputs}, _EXPECTED)
    _assert_fails(lambda: w.assert_legacy_path_only(_SOURCES, _EXPECTED))


def test_scenario_2_fallback_only() -> None:
    w = KernelWitness()
    outputs = _drive(w, None)
    assert outputs.column("c").to_pylist() == ["REDACTED", "REDACTED", "REDACTED"]
    w.assert_covered(_SOURCES, _EXPECTED)
    w.assert_legacy_path_only(_SOURCES, _EXPECTED)
    _assert_fails(lambda: w.assert_native_ran(_SOURCES, {"t": outputs}, _EXPECTED))


def test_scenario_3_native_attempted_then_fallback_supplied_result() -> None:
    w = KernelWitness()
    attempt = w.native(_native_raises)
    fallback = w.legacy(_mask.redact_array)

    def dispatch(values: Any, **kwargs: Any) -> pa.Array:
        try:
            return attempt(values, **kwargs)
        except _NativeDeclinedError:
            return fallback(values, **kwargs)

    outputs = _drive(w, {"redact_array": dispatch})
    # The native path was entered, but the fallback produced the output.
    assert w.native_attempts == 1
    assert outputs.column("c").to_pylist() == ["REDACTED", "REDACTED", "REDACTED"]
    w.assert_covered(_SOURCES, _EXPECTED)
    _assert_fails(lambda: w.assert_native_ran(_SOURCES, {"t": outputs}, _EXPECTED))
    # A native attempt also disqualifies the legacy-only claim.
    _assert_fails(lambda: w.assert_legacy_path_only(_SOURCES, _EXPECTED))


def test_scenario_4_missing_observation() -> None:
    w = KernelWitness()
    with w.observe():
        pass
    assert w.observations == []
    _assert_fails(lambda: w.assert_covered(_SOURCES, _EXPECTED))
    _assert_fails(lambda: w.assert_legacy_path_only(_SOURCES, _EXPECTED))
    _assert_fails(lambda: w.assert_native_ran(_SOURCES, {"t": _SOURCES["t"]}, _EXPECTED))


def test_native_result_that_is_not_the_output_fails() -> None:
    w = KernelWitness()
    _drive(w, {"redact_array": w.native(_native_double)})
    # The native call happened, but the emitted column is something else.
    elsewhere = pa.table({"c": pa.array(["x", "y", "z"], type=pa.string())})
    _assert_fails(lambda: w.assert_native_ran(_SOURCES, {"t": elsewhere}, _EXPECTED))


def test_untagged_result_is_unknown_and_satisfies_neither() -> None:
    w = KernelWitness()
    outputs = _drive(w, {"redact_array": _native_double})
    assert [o.backend for o in w.observations] == ["unknown"]
    _assert_fails(lambda: w.assert_legacy_path_only(_SOURCES, _EXPECTED))
    _assert_fails(lambda: w.assert_native_ran(_SOURCES, {"t": outputs}, _EXPECTED))


def test_observation_for_a_different_column_is_misattribution() -> None:
    w = KernelWitness()
    _drive(w, None)
    other = [ExpectedMask("t", "d", "redact_array")]
    sources = {"t": pa.table({"d": pa.array(["q", "r", "s"], type=pa.string())})}
    _assert_fails(lambda: w.assert_covered(sources, other))


def test_wrong_entrypoint_is_not_coverage() -> None:
    w = KernelWitness()
    _drive(w, None)
    _assert_fails(lambda: w.assert_covered(_SOURCES, [ExpectedMask("t", "c", "hash_array")]))


def test_spies_are_removed_after_the_block() -> None:
    before = {name: getattr(_mask, name) for name in ENTRYPOINTS}
    w = KernelWitness()
    _drive(w, None)
    assert {name: getattr(_mask, name) for name in ENTRYPOINTS} == before

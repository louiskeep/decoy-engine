"""The real-kernel suites' companion gate: skip only on ABSENT, fail on PRESENT-BUT-BROKEN.

Runs on every box, companion or not, by simulating each state: a missing distribution, an
installed binary reporting another ABI, an installed binary that fails the loader's
known-answer self-test, and (where one is installed) the real companion. The last two tests pin
that the real-kernel suites actually route their companion check through `require_companion`, so
a broken companion makes them FAIL rather than vacuously skip.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native import _crypto_ext
from tests.parity import _companion_gate as gate

OTHER_ABI = "decoy-native-abi-2"

# The real-kernel suites whose companion gate this file pins: each must skip on an absent
# companion but FAIL on an installed-but-broken one, never skipping it away.
REAL_KERNEL_SUITES = [
    "tests.parity.test_phase_d_dcomp_composite_ooc_real",
    "tests.parity.test_phase_d_dcomp_composite_seq_real",
]


def _fake_companion(monkeypatch: pytest.MonkeyPatch, *, abi: str, derive: Any) -> None:
    """Install a fake `decoy_engine_native` package whose `_kernel` reports `abi` and derives
    with `derive`, where the loader and `find_spec` look them up."""
    package = ModuleType(gate.DISTRIBUTION)
    package.__spec__ = ModuleSpec(gate.DISTRIBUTION, loader=None)
    package.__path__ = []  # type: ignore[attr-defined]
    kernel = ModuleType(f"{gate.DISTRIBUTION}._kernel")
    kernel.__spec__ = ModuleSpec(f"{gate.DISTRIBUTION}._kernel", loader=None)
    kernel.abi_version = lambda: abi  # type: ignore[attr-defined]
    kernel.derive_batch = derive  # type: ignore[attr-defined]
    package._kernel = kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, gate.DISTRIBUTION, package)
    monkeypatch.setitem(sys.modules, f"{gate.DISTRIBUTION}._kernel", kernel)


def _reference_derive(values: Any, **kw: Any) -> pa.Array:
    from decoy_engine.kernel import hash_array

    return hash_array(
        values, seed=kw["mask_key"], namespace=kw["namespace"], truncate=kw["truncate"]
    )


def _wrong_derive(values: Any, **kw: Any) -> pa.Array:
    return pa.array(["not-the-reference-token"] * len(values), pa.string())


def test_an_absent_companion_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "find_spec", lambda name: None)
    assert gate.companion_state() == gate.CompanionState(installed=False)
    with pytest.raises(pytest.skip.Exception):
        gate.require_companion()


def test_an_installed_companion_at_another_abi_fails_and_does_not_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_companion(monkeypatch, abi=OTHER_ABI, derive=_reference_derive)
    state = gate.companion_state()
    assert state.installed and state.broken
    assert OTHER_ABI in (state.error or "")
    with pytest.raises(pytest.fail.Exception, match=OTHER_ABI):
        gate.require_companion()


def test_an_installed_companion_that_fails_its_self_test_fails_and_does_not_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_companion(monkeypatch, abi=_crypto_ext._EXPECTED_ABI_VERSION, derive=_wrong_derive)
    state = gate.companion_state()
    assert state.installed and state.broken
    with pytest.raises(pytest.fail.Exception, match="does not load"):
        gate.require_companion()


def test_a_fake_companion_at_the_engine_abi_that_reproduces_the_reference_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the two failures above: the same fake with the right ABI and the reference
    derivation is accepted, so the fakes fail for the reason under test and nothing else."""
    _fake_companion(monkeypatch, abi=_crypto_ext._EXPECTED_ABI_VERSION, derive=_reference_derive)
    assert gate.companion_state().ready
    assert gate.require_companion().ready


@pytest.mark.skipif(gate.companion_state().absent, reason="no companion installed on this box")
def test_the_installed_companion_is_ready_or_the_gate_fails() -> None:
    """On a box with a companion: it loads at the engine's ABI, or this test fails (not skips)."""
    assert gate.require_companion().ready


def _suite_gate(module_name: str) -> Callable[[], Any]:
    """The autouse `_companion` gate fixture a real-kernel suite installs, as a plain callable.
    Reaching it through the imported module is the pin: a suite that drops the fixture for a
    module-level companion skip has no `_companion` to find, and this lookup fails loudly."""
    module = importlib.import_module(module_name)
    return module._companion.__wrapped__


@pytest.mark.parametrize("suite", REAL_KERNEL_SUITES)
def test_a_real_kernel_suite_fails_not_skips_on_a_broken_companion(
    suite: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vacuous-skip regression class: an installed-but-broken companion (here, wrong ABI)
    must make the suite's gate FAIL, so a stale binary cannot turn the suite green by skipping."""
    gate_fixture = _suite_gate(suite)
    _fake_companion(monkeypatch, abi=OTHER_ABI, derive=_reference_derive)
    with pytest.raises(pytest.fail.Exception, match=OTHER_ABI):
        gate_fixture()


@pytest.mark.parametrize("suite", REAL_KERNEL_SUITES)
def test_a_real_kernel_suite_skips_on_an_absent_companion(
    suite: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case a real-kernel suite may skip: nothing installed, nothing to prove."""
    gate_fixture = _suite_gate(suite)
    monkeypatch.setattr(gate, "find_spec", lambda name: None)
    with pytest.raises(pytest.skip.Exception):
        gate_fixture()

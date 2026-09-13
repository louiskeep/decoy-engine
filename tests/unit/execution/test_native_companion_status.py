"""Tests for `native_companion_status` (Task 3.2a): the public, read-only probe a
platform startup gate depends on instead of the private crypto/index loaders.

Mirrors `tests/native/test_crypto_ext_loader.py`'s injection pattern (a fake
`decoy_engine_native` / `decoy_engine_native._kernel` pair installed via
`sys.modules`) so every non-present-ok stage is exercised deterministically
regardless of whether the real companion is installed in this environment.
`importlib.util.find_spec` is patched directly for the absent-vs-present split,
since a `sys.modules` entry alone does not control what `find_spec` reports for
an already-imported name (it reads `module.__spec__`, which a bare
`types.ModuleType` does not carry).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
import types
from collections.abc import Callable

import pyarrow as pa
import pytest

from decoy_engine import NativeCompanionStatus as PublicNativeCompanionStatus
from decoy_engine import native_companion_status as public_native_companion_status
from decoy_engine.execution.native._companion_status import (
    NativeCompanionCheckError,
    NativeCompanionStatus,
    native_companion_status,
)
from decoy_engine.execution.native._crypto_ext import _EXPECTED_ABI_VERSION
from decoy_engine.execution.native._crypto_reference import reference_keyed_derivation
from decoy_engine.execution.native._index_ext import (
    INDEX_KAT,
    reference_index_derivation,
)

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None

# Sentinel: omit the attribute entirely, distinct from installing a stub that
# would fail if called -- lets a test exercise the loader's missing-entry-point
# branch (mirrors `_NO_DERIVE_BATCH` in test_crypto_ext_loader.py).
_NO_ENTRY_POINT = object()


def _patch_find_spec(
    monkeypatch: pytest.MonkeyPatch, *, present: bool, raises: bool = False
) -> None:
    def fake_find_spec(name: str, package: str | None = None) -> object | None:
        if raises:
            raise ModuleNotFoundError(name)
        if name == "decoy_engine_native" and present:
            return object()
        return None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


def _good_derive_batch() -> Callable[..., pa.Array]:
    reference = reference_keyed_derivation()

    def _fn(values: pa.Array, **kwargs: object) -> pa.Array:
        return reference.derive_batch(values, **kwargs)  # type: ignore[arg-type]

    return _fn


def _good_derive_index_batch() -> Callable[..., pa.Array]:
    reference = reference_index_derivation()

    def _fn(values: pa.Array, **kwargs: object) -> pa.Array:
        return reference.derive_index_batch(values, **kwargs)  # type: ignore[arg-type]

    return _fn


def _install_fake_kernel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    abi: str = _EXPECTED_ABI_VERSION,
    abi_raises: bool = False,
    derive_batch: Callable[..., pa.Array] | object | None = None,
    derive_index_batch: Callable[..., pa.Array] | object | None = None,
) -> None:
    """Inject a stand-in `decoy_engine_native._kernel` via `sys.modules`.

    `derive_batch` / `derive_index_batch` default to a working reference-backed
    entry point (so a test that only cares about the OTHER stage gets a passing
    KAT for the one it does not touch); pass `_NO_ENTRY_POINT` to omit the
    attribute, or any other value to install it verbatim (a wrong-value function,
    a raising one, a non-callable). Also patches `find_spec` to report the
    companion present, matching the module actually being importable."""
    _patch_find_spec(monkeypatch, present=True)

    def _abi_version() -> str:
        if abi_raises:
            raise RuntimeError("simulated abi_version() failure")
        return abi

    fake_kernel = types.ModuleType("decoy_engine_native._kernel")
    fake_kernel.abi_version = _abi_version  # type: ignore[attr-defined]
    if derive_batch is not _NO_ENTRY_POINT:
        fake_kernel.derive_batch = derive_batch or _good_derive_batch()  # type: ignore[attr-defined]
    if derive_index_batch is not _NO_ENTRY_POINT:
        fake_kernel.derive_index_batch = (  # type: ignore[attr-defined]
            derive_index_batch or _good_derive_index_batch()
        )
    fake_pkg = types.ModuleType("decoy_engine_native")
    fake_pkg._kernel = fake_kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", fake_pkg)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", fake_kernel)


# ---------------------------------------------------------------------------
# absent
# ---------------------------------------------------------------------------


def test_absent_when_find_spec_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_find_spec(monkeypatch, present=False)
    status = native_companion_status()
    assert status.present is False
    assert status.ok is False
    assert status.reason == "absent"
    assert status.abi_expected == _EXPECTED_ABI_VERSION
    assert status.abi_actual is None
    assert status.cause is not None
    assert isinstance(status.cause, NativeCompanionCheckError)
    assert status.cause.reason == "absent"


def test_absent_when_find_spec_raises_module_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # find_spec on a dotted name can raise ModuleNotFoundError if a parent package
    # in the path fails to import; the top-level module name here means this is a
    # defensive branch, but the caught exception must still be preserved as `cause`.
    _patch_find_spec(monkeypatch, present=False, raises=True)
    status = native_companion_status()
    assert status.present is False
    assert status.reason == "absent"
    assert isinstance(status.cause, ModuleNotFoundError)


def test_load_error_when_find_spec_raises_non_module_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # find_spec can raise beyond ModuleNotFoundError (e.g. ValueError when an
    # already-imported parent's __spec__ is None). The never-raises contract must
    # hold: classify as load-error (machinery present but broken), not absent.
    def _raise_value_error(name: str, package: str | None = None) -> object | None:
        raise ValueError("__spec__ is not set")

    monkeypatch.setattr(importlib.util, "find_spec", _raise_value_error)
    status = native_companion_status()
    assert status.ok is False
    assert status.reason == "load-error"
    assert isinstance(status.cause, ValueError)


def test_load_error_when_find_spec_module_not_found_names_foreign_dep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ModuleNotFoundError naming a DIFFERENT module (a broken dependency of the
    # companion's import machinery, not decoy_engine_native itself) is a
    # load-error, not absence -- the classification inspects exc.name.
    def _raise_foreign(name: str, package: str | None = None) -> object | None:
        raise ModuleNotFoundError("No module named 'some_broken_dep'", name="some_broken_dep")

    monkeypatch.setattr(importlib.util, "find_spec", _raise_foreign)
    status = native_companion_status()
    assert status.ok is False
    assert status.reason == "load-error"
    assert isinstance(status.cause, ModuleNotFoundError)


def test_version_lookup_failure_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed metadata record (any importlib.metadata error, not just
    # PackageNotFoundError) must not break the probe: version degrades to None.
    _install_fake_kernel(monkeypatch)

    def _boom(name: str) -> str:
        raise RuntimeError("corrupt metadata record")

    monkeypatch.setattr(importlib.metadata, "version", _boom)
    status = native_companion_status()
    assert status.reason == "present-ok"
    assert status.ok is True
    assert status.version is None


# ---------------------------------------------------------------------------
# load-error
# ---------------------------------------------------------------------------


def test_load_error_when_kernel_submodule_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # sys.modules[name] = None makes the import machinery raise ModuleNotFoundError
    # for that exact name, simulating a companion whose top package exists but whose
    # `_kernel` submodule fails to import (an undefined symbol / bad dynamic dep).
    _patch_find_spec(monkeypatch, present=True)
    fake_pkg = types.ModuleType("decoy_engine_native")
    monkeypatch.setitem(sys.modules, "decoy_engine_native", fake_pkg)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", None)

    status = native_companion_status()
    assert status.present is True
    assert status.ok is False
    assert status.reason == "load-error"
    assert status.abi_actual is None
    assert isinstance(status.cause, ModuleNotFoundError)


def test_load_error_when_abi_version_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_kernel(monkeypatch, abi_raises=True)
    status = native_companion_status()
    assert status.present is True
    assert status.ok is False
    assert status.reason == "load-error"
    assert status.abi_actual is None
    assert isinstance(status.cause, RuntimeError)


def test_load_error_when_derive_batch_entry_point_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel(monkeypatch, derive_batch=_NO_ENTRY_POINT)
    status = native_companion_status()
    assert status.present is True
    assert status.ok is False
    assert status.reason == "load-error"
    assert status.abi_actual == _EXPECTED_ABI_VERSION
    assert isinstance(status.cause, AttributeError)


def test_load_error_when_derive_batch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raising(*args: object, **kwargs: object) -> pa.Array:
        raise RuntimeError("simulated derive_batch failure")

    _install_fake_kernel(monkeypatch, derive_batch=_raising)
    status = native_companion_status()
    assert status.reason == "load-error"
    assert isinstance(status.cause, RuntimeError)


def test_load_error_when_derive_index_batch_entry_point_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A companion built before the index kernel existed: still a valid abi-2 build
    # for the hash route, but incomplete. This is the "partially-capable companion
    # must not report present-ok" case: the hash KAT passes, but the overall status
    # must still be not-ok.
    _install_fake_kernel(monkeypatch, derive_index_batch=_NO_ENTRY_POINT)
    status = native_companion_status()
    assert status.present is True
    assert status.ok is False
    assert status.reason == "load-error"
    assert status.abi_actual == _EXPECTED_ABI_VERSION
    assert isinstance(status.cause, AttributeError)


# ---------------------------------------------------------------------------
# abi-mismatch
# ---------------------------------------------------------------------------


def test_abi_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-0-stale")
    status = native_companion_status()
    assert status.present is True
    assert status.ok is False
    assert status.reason == "abi-mismatch"
    assert status.abi_expected == _EXPECTED_ABI_VERSION
    assert status.abi_actual == "decoy-native-abi-0-stale"
    assert isinstance(status.cause, NativeCompanionCheckError)
    assert status.cause.reason == "abi-mismatch"
    assert status.cause.abi_expected == _EXPECTED_ABI_VERSION
    assert status.cause.abi_actual == "decoy-native-abi-0-stale"


# ---------------------------------------------------------------------------
# kat-corrupt
# ---------------------------------------------------------------------------


def test_kat_corrupt_when_hash_kat_wrong_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def _wrong(values: pa.Array, **kwargs: object) -> pa.Array:
        return pa.array(["deadbeef"] * len(values), type=pa.string())

    _install_fake_kernel(monkeypatch, derive_batch=_wrong)
    status = native_companion_status()
    assert status.present is True
    assert status.ok is False
    assert status.reason == "kat-corrupt"
    assert status.abi_actual == _EXPECTED_ABI_VERSION
    assert isinstance(status.cause, NativeCompanionCheckError)
    assert status.cause.reason == "kat-corrupt"


def test_kat_corrupt_when_index_kat_wrong_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def _wrong(values: pa.Array, **kwargs: object) -> pa.Array:
        return pa.array([0] * len(INDEX_KAT.values), type=pa.uint64())

    _install_fake_kernel(monkeypatch, derive_index_batch=_wrong)
    status = native_companion_status()
    assert status.present is True
    assert status.ok is False
    assert status.reason == "kat-corrupt"
    assert isinstance(status.cause, NativeCompanionCheckError)
    assert status.cause.reason == "kat-corrupt"


# ---------------------------------------------------------------------------
# present-ok
# ---------------------------------------------------------------------------


def test_present_ok_when_both_kernels_reproduce_their_kats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel(monkeypatch)
    status = native_companion_status()
    assert status.present is True
    assert status.ok is True
    assert status.reason == "present-ok"
    assert status.abi_expected == _EXPECTED_ABI_VERSION
    assert status.abi_actual == _EXPECTED_ABI_VERSION
    assert status.cause is None


@pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)
def test_present_ok_against_the_real_companion() -> None:
    status = native_companion_status()
    assert status.reason == "present-ok"
    assert status.ok is True
    assert status.present is True
    assert status.cause is None


# ---------------------------------------------------------------------------
# Cross-cutting contract + public wiring.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "install",
    [
        lambda mp: _patch_find_spec(mp, present=False),
        lambda mp: _install_fake_kernel(mp, abi_raises=True),
        lambda mp: _install_fake_kernel(mp, abi="decoy-native-abi-0-stale"),
        lambda mp: _install_fake_kernel(
            mp, derive_batch=lambda values, **k: pa.array(["x"] * len(values), type=pa.string())
        ),
    ],
    ids=["absent", "load-error", "abi-mismatch", "kat-corrupt"],
)
def test_cause_is_never_none_when_not_ok(
    monkeypatch: pytest.MonkeyPatch, install: Callable[[pytest.MonkeyPatch], None]
) -> None:
    install(monkeypatch)
    status = native_companion_status()
    assert status.ok is False
    assert status.cause is not None


def test_status_dataclass_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_find_spec(monkeypatch, present=False)
    status = native_companion_status()
    with pytest.raises(AttributeError):
        status.ok = True  # type: ignore[misc]


def test_public_import_is_the_same_object() -> None:
    # Governance: the platform depends on decoy_engine.native_companion_status /
    # decoy_engine.NativeCompanionStatus, not the private execution.native module.
    assert public_native_companion_status is native_companion_status
    assert PublicNativeCompanionStatus is NativeCompanionStatus

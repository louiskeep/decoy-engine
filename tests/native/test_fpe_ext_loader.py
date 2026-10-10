"""Tests for `load_compiled_fpe_kernel` (C6a): the ABI check, the load-time FPE known-answer
self-test, the fail-before-output contract, and the compiled kernel's byte + status parity
against the pure-Python reference `_ReferenceFpe`.

Loader-mechanics tests inject a stand-in `decoy_engine_native._kernel` via `sys.modules` so they
run with or without the real companion. Kernel-behavior tests need the real compiled kernel and
skip when it is absent (the companion-present CI job covers them).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable

import pyarrow as pa
import pytest

from decoy_engine.determinism import DeterminismError
from decoy_engine.errors import MaskKeyRequiredError
from decoy_engine.execution.native._crypto_ext import (
    _EXPECTED_ABI_VERSION,
    FPE_KAT,
    CryptoExtensionUnavailableError,
    FpeConfig,
)
from decoy_engine.execution.native._crypto_reference import reference_fpe
from decoy_engine.execution.native._fpe_ext import (
    _translate_compiled_fpe_error,
    load_compiled_fpe_kernel,
    native_fpe,
)

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_MASK_KEY = bytes(range(32))

present = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)

_NO_FPE = object()


def _install_fake_kernel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    abi: str,
    fpe_transform_batch: Callable[..., object] | object | None = None,
    abi_raises: bool = False,
) -> None:
    def _abi_version() -> str:
        if abi_raises:
            raise RuntimeError("simulated abi_version() failure")
        return abi

    fake_kernel = types.ModuleType("decoy_engine_native._kernel")
    fake_kernel.abi_version = _abi_version  # type: ignore[attr-defined]
    if fpe_transform_batch is not _NO_FPE:
        fake_kernel.fpe_transform_batch = fpe_transform_batch or (  # type: ignore[attr-defined]
            lambda *a, **k: pytest.fail("fpe_transform_batch should not be called in this test")
        )
    fake_pkg = types.ModuleType("decoy_engine_native")
    fake_pkg._kernel = fake_kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", fake_pkg)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", fake_kernel)


# --- _translate_compiled_fpe_error -----------------------------------------


def test_translate_seed_and_namespace_errors_to_determinism_error() -> None:
    for code in ("seed_wrong_length", "namespace_empty"):
        exc = ValueError(f"{code}: detail text: with colon")
        translated = _translate_compiled_fpe_error(exc)
        assert type(translated) is DeterminismError
        assert translated.code == code
        assert translated.message == "detail text: with colon"


def test_translate_passes_through_unrecognized_code() -> None:
    exc = ValueError("fpe_input_not_string: got another Arrow type")
    assert _translate_compiled_fpe_error(exc) is exc


# --- loader mechanics (fake kernel) ----------------------------------------


def test_wrong_abi_tag_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-0-stale")
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_fpe_kernel()


def test_abi_version_raising_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_kernel(monkeypatch, abi=_EXPECTED_ABI_VERSION, abi_raises=True)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_fpe_kernel()


def test_missing_entry_point_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_kernel(monkeypatch, abi=_EXPECTED_ABI_VERSION, fpe_transform_batch=_NO_FPE)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_fpe_kernel()


def test_wrong_output_fails_self_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def _wrong(values: pa.Array, **kwargs: object) -> tuple[pa.Array, list[object]]:
        return pa.array(["nope"], type=pa.string()), []

    _install_fake_kernel(monkeypatch, abi=_EXPECTED_ABI_VERSION, fpe_transform_batch=_wrong)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_fpe_kernel()


# --- kernel behavior (real companion) --------------------------------------


@present
def test_loads_and_reproduces_every_fpe_kat() -> None:
    kernel = load_compiled_fpe_kernel()
    for vec in FPE_KAT:
        result = kernel.encrypt_batch(
            pa.array([vec.plaintext], type=pa.string()),
            mask_key=vec.mask_key,
            namespace=vec.namespace,
            tweak_column=vec.tweak_column,
            config=vec.config,
        )
        assert result.values.to_pylist() == [vec.ciphertext], vec
        assert result.errors == ()


@present
@pytest.mark.parametrize(
    "config",
    [
        FpeConfig(charset="digits"),
        FpeConfig(charset="digits", preserve_separators=False),
        FpeConfig(charset="alphanum"),
        FpeConfig(charset="digits", validate_luhn=True),
        FpeConfig(charset="digits", join_group="grp"),
    ],
)
def test_compiled_matches_reference_values_and_status(config: FpeConfig) -> None:
    values = pa.array(
        ["123456789", None, "", "123-45-6789", "----------", "4111111111111111"],
        type=pa.string(),
    )
    compiled = load_compiled_fpe_kernel()
    reference = reference_fpe()
    got = compiled.encrypt_batch(
        values, mask_key=_MASK_KEY, namespace="people.ssn", tweak_column="ssn", config=config
    )
    want = reference.encrypt_batch(
        values, mask_key=_MASK_KEY, namespace="people.ssn", tweak_column="ssn", config=config
    )
    assert got.values.to_pylist() == want.values.to_pylist()
    assert got.errors == want.errors


@present
def test_round_trip_restores_originals() -> None:
    config = FpeConfig(charset="digits")
    values = pa.array(["123-45-6789", "000111222", None, ""], type=pa.string())
    ct = native_fpe(
        values, mask_key=_MASK_KEY, namespace="people.ssn", tweak_column="ssn", config=config
    )
    pt = native_fpe(
        ct.values,
        mask_key=_MASK_KEY,
        namespace="people.ssn",
        tweak_column="ssn",
        config=config,
        forward=False,
    )
    assert pt.values.to_pylist() == values.to_pylist()


@present
def test_empty_mask_key_fails_closed_before_any_row() -> None:
    kernel = load_compiled_fpe_kernel()
    with pytest.raises(MaskKeyRequiredError):
        kernel.encrypt_batch(
            pa.array(["123456789"], type=pa.string()),
            mask_key=b"",
            namespace="people.ssn",
            tweak_column="ssn",
            config=FpeConfig(charset="digits"),
        )

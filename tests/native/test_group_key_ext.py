"""Tests for `load_compiled_raw_hex_kernel` (native group_key): the loader, the
shared ABI check, capability detection of the additive `derive_hex_raw_batch`
symbol, its fail-before-output contract, and the pure-Python reference oracle.

Mirrors `test_index_ext.py`'s structure. Loader-mechanics tests inject a
stand-in module tree via `sys.modules` so they run identically whether or not
the real companion is installed, and assert the STAGE that failed (missing
symbol vs self-test) via distinct messages. Kernel-behavior tests need the real
compiled kernel and skip when it is absent. Embedding-drift + reference-only
tests need no companion at all.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable

import pyarrow as pa
import pytest

from decoy_engine.determinism import DeterminismError
from decoy_engine.determinism._derive import derive
from decoy_engine.errors import MaskKeyRequiredError
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._group_key_ext import (
    RAW_HEX_KAT,
    CryptoExtensionUnavailableError,
    _translate_compiled_raw_hex_kernel_error,
    load_compiled_raw_hex_kernel,
    reference_raw_hex_derivation,
)
from decoy_engine.generation.pool._errors import GenerationError

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)
# The compiled RAW-HEX kernel must be loadable, which needs a companion carrying
# the additive `derive_hex_raw_batch` symbol -- a present-but-older companion (no
# raw symbol) reports ok=False, so `find_spec` alone is not enough for the tests
# that load the real kernel.
_NEEDS_RAW_KERNEL = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled raw-hex kernel unavailable; the companion-present CI job covers this",
)


# ── Embedding-drift: the RAW_HEX_KAT constant must match the live primitive ──


def test_embedded_kat_matches_the_live_python_reference() -> None:
    n_bytes = RAW_HEX_KAT.hex_chars // 2
    expected = tuple(
        derive(RAW_HEX_KAT.mask_key, RAW_HEX_KAT.namespace, v.encode("utf-8"))[:n_bytes].hex()
        for v in RAW_HEX_KAT.values
    )
    assert expected == RAW_HEX_KAT.expected


def test_embedded_kat_matches_reference_wrapper() -> None:
    out = reference_raw_hex_derivation().derive_hex_raw_batch(
        pa.array(RAW_HEX_KAT.values, type=pa.string()),
        mask_key=RAW_HEX_KAT.mask_key,
        namespace=RAW_HEX_KAT.namespace,
        hex_chars=RAW_HEX_KAT.hex_chars,
    )
    assert out.type == pa.string()
    assert tuple(out.to_pylist()) == RAW_HEX_KAT.expected


# ── reference kernel works with no companion at all ──────────────────────────


def test_reference_is_pure_python_and_null_safe() -> None:
    ref = reference_raw_hex_derivation()
    out = ref.derive_hex_raw_batch(
        pa.array(["alice", None, "bob"], type=pa.string()),
        mask_key=bytes(range(32)),
        namespace="group_key/x",
        hex_chars=16,
    )
    assert out.type == pa.string()
    values = out.to_pylist()
    assert values[1] is None  # null in -> null out (defensive; astype(str) leaves none)
    assert all(isinstance(v, str) and len(v) == 16 for v in (values[0], values[2]))


def test_reference_requires_mask_key() -> None:
    with pytest.raises(MaskKeyRequiredError):
        reference_raw_hex_derivation().derive_hex_raw_batch(
            pa.array(["alice"], type=pa.string()),
            mask_key=None,
            namespace="group_key/x",
            hex_chars=16,
        )


# ── Error translation (drop-in exception-type contract) ──────────────────────


@pytest.mark.parametrize("code", ["seed_wrong_length", "namespace_empty"])
def test_translate_determinism_codes(code: str) -> None:
    assert isinstance(
        _translate_compiled_raw_hex_kernel_error(ValueError(f"{code}: d")), DeterminismError
    )


@pytest.mark.parametrize(
    "code", ["mixed_object_not_native", "group_key_input_not_string", "group_key_hex_chars_invalid"]
)
def test_translate_generation_codes(code: str) -> None:
    assert isinstance(
        _translate_compiled_raw_hex_kernel_error(ValueError(f"{code}: d")), GenerationError
    )


def test_translate_unrecognized_code_passes_through() -> None:
    exc = ValueError("some_unknown_code: detail")
    assert _translate_compiled_raw_hex_kernel_error(exc) is exc


# ── Loader mechanics via a stand-in kernel module ────────────────────────────


def _install_fake_kernel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    abi: str,
    derive_hex_raw_batch: Callable[..., pa.Array] | object | None = None,
) -> None:
    fake_kernel = types.ModuleType("decoy_engine_native._kernel")
    fake_kernel.abi_version = lambda: abi  # type: ignore[attr-defined]
    if derive_hex_raw_batch is not None:
        fake_kernel.derive_hex_raw_batch = derive_hex_raw_batch  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("decoy_engine_native")
    fake_pkg._kernel = fake_kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", fake_pkg)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", fake_kernel)


def test_absent_companion_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "decoy_engine_native", None)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_raw_hex_kernel()


def test_wrong_abi_tag_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-0-stale")
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_raw_hex_kernel()


def test_missing_symbol_fails_at_the_missing_symbol_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """An abi-2 companion that predates the raw-hex kernel has no
    `derive_hex_raw_batch`. This must fail with the distinct missing-symbol
    message, before the self-test could run."""
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_hex_raw_batch=None)
    with pytest.raises(CryptoExtensionUnavailableError) as exc_info:
        load_compiled_raw_hex_kernel()
    message = str(exc_info.value)
    assert "lacks derive_hex_raw_batch" in message
    assert "self-test" not in message


def test_present_but_wrong_values_fails_at_the_self_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def _wrong(values: pa.Array, **kwargs: object) -> pa.Array:
        return pa.array(["deadbeef"] * len(values), type=pa.string())

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_hex_raw_batch=_wrong)
    with pytest.raises(CryptoExtensionUnavailableError) as exc_info:
        load_compiled_raw_hex_kernel()
    message = str(exc_info.value)
    assert "self-test" in message
    assert "lacks derive_hex_raw_batch" not in message


def test_present_but_raising_fails_at_the_self_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raises(values: pa.Array, **kwargs: object) -> pa.Array:
        raise RuntimeError("simulated companion failure")

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_hex_raw_batch=_raises)
    with pytest.raises(CryptoExtensionUnavailableError) as exc_info:
        load_compiled_raw_hex_kernel()
    assert "self-test" in str(exc_info.value)


def test_present_but_wrong_arrow_type_fails_the_self_test(monkeypatch: pytest.MonkeyPatch) -> None:
    def _wrong_type(values: pa.Array, **kwargs: object) -> pa.Array:
        # Right values, wrong Arrow type (large_string) -- must not slip past.
        return pa.array(list(RAW_HEX_KAT.expected), type=pa.large_string())

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_hex_raw_batch=_wrong_type)
    with pytest.raises(CryptoExtensionUnavailableError) as exc_info:
        load_compiled_raw_hex_kernel()
    assert "self-test" in str(exc_info.value)


def test_matching_abi_and_kat_returns_a_working_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = reference_raw_hex_derivation()

    def _recording(values: pa.Array, **kwargs: object) -> pa.Array:
        return reference.derive_hex_raw_batch(values, **kwargs)  # type: ignore[arg-type]

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_hex_raw_batch=_recording)
    kernel = load_compiled_raw_hex_kernel()
    out = kernel.derive_hex_raw_batch(
        pa.array(["alice", "bob"], type=pa.string()),
        mask_key=bytes(range(32)),
        namespace="group_key/x",
        hex_chars=16,
    )
    assert out.type == pa.string()


def test_wrapper_requires_mask_key_before_kernel_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loaded wrapper's own `_require_mask_key` fires before the compiled
    entry point is ever called (a spy that would raise if reached)."""

    def _never(values: pa.Array, **kwargs: object) -> pa.Array:  # pragma: no cover
        raise AssertionError("kernel must not be called when mask_key is missing")

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_hex_raw_batch=_recording_kat)
    kernel = load_compiled_raw_hex_kernel()
    kernel._derive_hex_raw_batch_fn = _never  # type: ignore[attr-defined]
    with pytest.raises(MaskKeyRequiredError):
        kernel.derive_hex_raw_batch(
            pa.array(["a"], type=pa.string()), mask_key=None, namespace="ns", hex_chars=16
        )


def _recording_kat(values: pa.Array, **kwargs: object) -> pa.Array:
    return reference_raw_hex_derivation().derive_hex_raw_batch(values, **kwargs)  # type: ignore[arg-type]


# ── Real compiled kernel (companion present) ─────────────────────────────────


@_NEEDS_RAW_KERNEL
def test_compiled_matches_reference_and_kat() -> None:
    kernel = load_compiled_raw_hex_kernel()
    ref = reference_raw_hex_derivation()
    values = pa.array(["alice", "bob", "alice", "carol", "None", ""], type=pa.string())
    for hex_chars in (8, 16, 64):
        c = kernel.derive_hex_raw_batch(
            values, mask_key=bytes(range(32)), namespace="group_key/x", hex_chars=hex_chars
        )
        r = ref.derive_hex_raw_batch(
            values, mask_key=bytes(range(32)), namespace="group_key/x", hex_chars=hex_chars
        )
        assert c.type == pa.string()
        assert c.to_pylist() == r.to_pylist()
    kat = kernel.derive_hex_raw_batch(
        pa.array(RAW_HEX_KAT.values, type=pa.string()),
        mask_key=RAW_HEX_KAT.mask_key,
        namespace=RAW_HEX_KAT.namespace,
        hex_chars=RAW_HEX_KAT.hex_chars,
    )
    assert kat.to_pylist() == list(RAW_HEX_KAT.expected)


@_NEEDS_RAW_KERNEL
def test_compiled_missing_mask_key_raises() -> None:
    kernel = load_compiled_raw_hex_kernel()
    with pytest.raises(MaskKeyRequiredError):
        kernel.derive_hex_raw_batch(
            pa.array(["a"], type=pa.string()), mask_key=None, namespace="ns", hex_chars=16
        )

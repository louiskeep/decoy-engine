"""Tests for `load_compiled_crypto_kernel` (Task 2.3): the real load, the ABI-version
check, and the fail-before-output contract, replacing the Phase 0 always-raising stub.

Two groups:

- Loader-mechanics tests (absent companion, wrong ABI tag, a raising `abi_version()`)
  inject a stand-in module tree via `sys.modules` so they run identically whether or
  not the real `decoy-engine-native` companion happens to be installed.
- Kernel-behavior tests (byte parity, exception-type parity with the reference) need
  the real compiled kernel and are skipped when the companion is not installed; the
  companion-present CI job covers them, mirroring `test_native_ext_abi.py`'s pattern.
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
    HASH_KAT,
    CryptoExtensionUnavailableError,
    _translate_compiled_kernel_error,
    load_compiled_crypto_kernel,
    reference_keyed_derivation,
)
from decoy_engine.generation.pool._errors import GenerationError

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_MASK_KEY = bytes(range(32))
_NAMESPACE = "loader.parity"

# Sentinel: install a `_kernel` with NO `derive_batch` attribute at all, distinct
# from "install the default stub" (derive_batch=None) so a test can exercise the
# loader's missing-entry-point branch.
_NO_DERIVE_BATCH = object()

pytestmark_present = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)


def _install_fake_kernel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    abi: str,
    derive_batch: Callable[..., pa.Array] | object | None = None,
    abi_raises: bool = False,
) -> None:
    """Inject a stand-in `decoy_engine_native._kernel` module via `sys.modules`.

    Runs identically whether or not the real companion is installed: the injected
    entries shadow whatever `sys.modules` already holds for the duration of the test
    and monkeypatch restores the prior state afterward (real or absent).

    `derive_batch` controls the entry point: `None` installs a stub that fails if
    called (for tests that never reach the entry point, e.g. an ABI mismatch that
    raises first), `_NO_DERIVE_BATCH` omits the attribute entirely, and any other
    value (callable or not) is installed verbatim so a test can feed the loader a
    broken entry point. NB: `load_compiled_crypto_kernel` runs a load-time self-test,
    so a fake meant to load successfully must reproduce `HASH_KAT[0]` (see
    `_recording_reference_derive_batch`)."""

    def _abi_version() -> str:
        if abi_raises:
            raise RuntimeError("simulated abi_version() failure")
        return abi

    fake_kernel = types.ModuleType("decoy_engine_native._kernel")
    fake_kernel.abi_version = _abi_version  # type: ignore[attr-defined]
    if derive_batch is not _NO_DERIVE_BATCH:
        fake_kernel.derive_batch = derive_batch or (  # type: ignore[attr-defined]
            lambda *a, **k: pytest.fail("derive_batch should not be called in this test")
        )
    fake_pkg = types.ModuleType("decoy_engine_native")
    fake_pkg._kernel = fake_kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", fake_pkg)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", fake_kernel)


def _recording_reference_derive_batch(
    calls: list[dict[str, object]],
) -> Callable[..., pa.Array]:
    """A fake `derive_batch` that records its kwargs and delegates to the pure-Python
    reference, so it reproduces `HASH_KAT[0]` and passes the loader's self-test while
    still letting a test assert what the wrapper forwarded."""
    reference = reference_keyed_derivation()

    def _fn(values: pa.Array, **kwargs: object) -> pa.Array:
        calls.append(kwargs)
        return reference.derive_batch(values, **kwargs)  # type: ignore[arg-type]

    return _fn


# ---------------------------------------------------------------------------
# _translate_compiled_kernel_error: exercised directly, independent of the real
# compiled companion (it is a pure function over a ValueError's coded message).
# ---------------------------------------------------------------------------


def test_translate_seed_wrong_length_splits_on_the_first_colon_only() -> None:
    # The compiled kernel's message is "<code>: <detail>", and detail can itself embed
    # ": " (the FFI-import path wraps an arrow-rs error's Display text, which is not
    # guaranteed colon-free). Splitting on the FIRST occurrence, not the last, is what
    # keeps `code` exactly equal to the coded prefix regardless of what detail contains.
    exc = ValueError("seed_wrong_length: mask_key must be 8 or 32 bytes; got 20: see docs")
    translated = _translate_compiled_kernel_error(exc)
    assert type(translated) is DeterminismError
    assert translated.code == "seed_wrong_length"
    assert translated.message == "mask_key must be 8 or 32 bytes; got 20: see docs"


def test_translate_namespace_empty_splits_on_the_first_colon_only() -> None:
    exc = ValueError("namespace_empty: namespace must be non-empty: see docs")
    translated = _translate_compiled_kernel_error(exc)
    assert type(translated) is DeterminismError
    assert translated.code == "namespace_empty"
    assert translated.message == "namespace must be non-empty: see docs"


def test_translate_mixed_object_not_native_maps_to_coded_generation_error() -> None:
    # The kernel-behavior tests above assert only the exception TYPE for this mapping
    # (a live compiled-kernel rejection never carries an embedded colon in practice); this
    # pins the CODE too, directly, per the zero-unadjudicated-survivor bar's "type + code"
    # requirement for every rejection path.
    exc = ValueError("mixed_object_not_native: Arrow C Data Interface rejected the input: bad")
    translated = _translate_compiled_kernel_error(exc)
    assert type(translated) is GenerationError
    assert translated.code == "native_type_not_admitted"
    assert translated.message == "Arrow C Data Interface rejected the input: bad"


def test_translate_passes_through_an_unrecognized_code_unchanged() -> None:
    # A code this wrapper was never built to translate must surface exactly the original
    # exception object, not be silently swallowed, wrapped, or replaced.
    exc = ValueError("some_future_code: detail text")
    translated = _translate_compiled_kernel_error(exc)
    assert translated is exc


# ---------------------------------------------------------------------------
# Loader mechanics: absent companion, wrong ABI, a raising abi_version().
# ---------------------------------------------------------------------------


def test_absent_companion_raises_unavailable_not_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # sys.modules[name] = None makes the import machinery raise ImportError for that
    # name (a documented Python import-system behavior), simulating an uninstalled
    # companion deterministically regardless of the real environment.
    monkeypatch.setitem(sys.modules, "decoy_engine_native", None)

    import decoy_engine  # noqa: F401 -- the core must still import with the companion absent

    with pytest.raises(CryptoExtensionUnavailableError) as exc_info:
        load_compiled_crypto_kernel()
    assert exc_info.type is CryptoExtensionUnavailableError


def test_wrong_abi_tag_raises_unavailable_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-0-stale")
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_crypto_kernel()


def test_abi_version_call_raising_is_treated_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-1", abi_raises=True)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_crypto_kernel()


def test_matching_abi_but_missing_derive_batch_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A companion whose ABI tag matches but that exposes no `derive_batch` must fail
    # closed BEFORE returning, not leak a bare AttributeError from the return line.
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-1", derive_batch=_NO_DERIVE_BATCH)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_crypto_kernel()


def test_matching_abi_but_non_callable_derive_batch_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-callable `derive_batch` (a stale build shipping a data attribute, say)
    # must be rejected at load time rather than wrapped into a kernel that raises
    # TypeError only once the first batch is derived, after output would begin.
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-1", derive_batch=42)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_crypto_kernel()


def test_matching_abi_but_callable_dunder_none_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `callable(obj)` returns True when `type(obj).__call__` merely exists, even if
    # it is None: such an object passes a naive callability check yet raises TypeError
    # when called. The load-time self-test calls the entry point, so this is caught
    # and the loader fails closed rather than returning a kernel that dies mid-derive.
    class _CallDunderNone:
        __call__ = None

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-1", derive_batch=_CallDunderNone())
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_crypto_kernel()


def test_matching_abi_but_raising_derive_batch_descriptor_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `derive_batch` whose mere attribute access raises (a pathological descriptor)
    # must not leak that bare exception out of the loader. Resolving the entry point
    # inside the guarded self-test turns it into a fail-closed CryptoExtensionUnavailableError.
    class _RaisingKernel(types.ModuleType):
        def abi_version(self) -> str:
            return "decoy-native-abi-1"

        @property
        def derive_batch(self) -> Callable[..., pa.Array]:
            raise RuntimeError("simulated derive_batch descriptor failure")

    fake_kernel = _RaisingKernel("decoy_engine_native._kernel")
    fake_pkg = types.ModuleType("decoy_engine_native")
    fake_pkg._kernel = fake_kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", fake_pkg)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", fake_kernel)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_crypto_kernel()


def test_matching_abi_but_wrong_bytes_fails_load_time_self_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The realistic case the self-test exists for: a companion that imports and
    # reports the right ABI but derives the wrong bytes (a mis-built binary) is
    # rejected at load, never returned as a kernel that would emit wrong output.
    def _wrong(values: pa.Array, **kwargs: object) -> pa.Array:
        return pa.array(["deadbeef"] * len(values), type=pa.string())

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-1", derive_batch=_wrong)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_crypto_kernel()


def test_matching_abi_but_wrong_arrow_type_fails_load_time_self_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Right Python values, wrong Arrow type: an entry point returning large_string
    # where the reference returns string must be rejected at load, so a type mismatch
    # cannot ride in on a matching .to_pylist().
    reference = reference_keyed_derivation()

    def _wrong_type(values: pa.Array, **kwargs: object) -> pa.Array:
        correct = reference.derive_batch(values, **kwargs)  # type: ignore[arg-type]
        return pa.array(correct.to_pylist(), type=pa.large_string())

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-1", derive_batch=_wrong_type)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_crypto_kernel()


def test_matching_abi_tag_returns_a_working_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    _install_fake_kernel(
        monkeypatch,
        abi="decoy-native-abi-1",
        derive_batch=_recording_reference_derive_batch(calls),
    )
    kernel = load_compiled_crypto_kernel()
    array = pa.array(["alice"])
    out = kernel.derive_batch(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=8)

    reference_out = reference_keyed_derivation().derive_batch(
        array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=8
    )
    assert out.equals(reference_out)
    # The load-time self-test makes one call (HASH_KAT[0]); the real derive is the
    # last. Every keyword the reference contract carries must reach the entry point:
    # namespace, the resolved mask_key, and truncate (not just namespace).
    assert calls[-1]["namespace"] == _NAMESPACE
    assert calls[-1]["mask_key"] == _MASK_KEY
    assert calls[-1]["truncate"] == 8


# ---------------------------------------------------------------------------
# Kernel behavior: needs the real compiled companion.
# ---------------------------------------------------------------------------


@pytestmark_present
def test_loaded_kernel_matches_reference_on_a_small_array() -> None:
    kernel = load_compiled_crypto_kernel()
    array = pa.array(["alice", "bob", None, "carol"])

    reference_out = reference_keyed_derivation().derive_batch(
        array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None
    )
    loaded_out = kernel.derive_batch(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None)
    # Typed equality (Arrow type + logical values), stronger than .to_pylist(): this
    # catches type drift (a wrapper returning large_string where the reference returns
    # string) that a Python-value comparison would miss. We deliberately do NOT assert
    # physical buffer identity: a zero-copy slice with a nonzero offset but identical
    # logical values is equally correct output, so requiring byte-identical buffers
    # would over-specify the kernel's internal representation.
    assert loaded_out.type == reference_out.type
    assert loaded_out.equals(reference_out)


@pytestmark_present
def test_loaded_kernel_matches_reference_over_hash_kat_vectors() -> None:
    kernel = load_compiled_crypto_kernel()
    for vector in HASH_KAT:
        array = pa.array([vector.value])
        got = kernel.derive_batch(
            array,
            mask_key=vector.mask_key,
            namespace=vector.namespace,
            truncate=vector.truncate,
        )
        assert got.to_pylist() == [vector.expected], vector


@pytestmark_present
def test_loaded_kernel_is_type_and_value_identical_to_reference_over_hash_kat() -> None:
    # The permanent typed-parity regression: drive every shared keyed-derivation
    # vector through BOTH the loaded compiled kernel and the reference and require
    # Arrow type + logical-value equality, closing the type-drift gap the .to_pylist()
    # parity tests leave open (e.g. large_string vs string with matching values).
    kernel = load_compiled_crypto_kernel()
    reference = reference_keyed_derivation()
    for vector in HASH_KAT:
        array = pa.array([vector.value])
        loaded_out = kernel.derive_batch(
            array, mask_key=vector.mask_key, namespace=vector.namespace, truncate=vector.truncate
        )
        reference_out = reference.derive_batch(
            array, mask_key=vector.mask_key, namespace=vector.namespace, truncate=vector.truncate
        )
        assert loaded_out.type == reference_out.type, vector
        assert loaded_out.equals(reference_out), vector


@pytestmark_present
def test_loaded_kernel_matches_reference_over_a_chunked_array() -> None:
    # KernelInput admits pa.ChunkedArray; the wrapper combines chunks before handing
    # the array to the compiled entry point, which accepts only a single pa.Array.
    kernel = load_compiled_crypto_kernel()
    chunked = pa.chunked_array([["alice", "bob"], [None, "carol"]])

    reference_out = reference_keyed_derivation().derive_batch(
        chunked, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=8
    )
    loaded_out = kernel.derive_batch(chunked, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=8)
    assert loaded_out.to_pylist() == reference_out.to_pylist()


@pytestmark_present
@pytest.mark.parametrize("mask_key", [None, b""])
def test_missing_or_empty_mask_key_raises_mask_key_required_before_output(
    mask_key: bytes | None,
) -> None:
    kernel = load_compiled_crypto_kernel()
    array = pa.array(["alice"])

    with pytest.raises(MaskKeyRequiredError) as ref_exc:
        reference_keyed_derivation().derive_batch(
            array, mask_key=mask_key, namespace=_NAMESPACE, truncate=None
        )
    with pytest.raises(MaskKeyRequiredError) as loaded_exc:
        kernel.derive_batch(array, mask_key=mask_key, namespace=_NAMESPACE, truncate=None)

    assert type(loaded_exc.value) is type(ref_exc.value)


@pytestmark_present
def test_wrong_length_key_raises_same_type_as_reference() -> None:
    kernel = load_compiled_crypto_kernel()
    array = pa.array(["alice"])
    wrong_length_key = bytes(20)

    with pytest.raises(DeterminismError) as ref_exc:
        reference_keyed_derivation().derive_batch(
            array, mask_key=wrong_length_key, namespace=_NAMESPACE, truncate=None
        )
    assert ref_exc.value.code == "seed_wrong_length"

    with pytest.raises(DeterminismError) as loaded_exc:
        kernel.derive_batch(array, mask_key=wrong_length_key, namespace=_NAMESPACE, truncate=None)
    assert loaded_exc.value.code == "seed_wrong_length"


@pytestmark_present
def test_empty_namespace_raises_same_type_as_reference() -> None:
    kernel = load_compiled_crypto_kernel()
    array = pa.array(["alice"])

    with pytest.raises(DeterminismError) as ref_exc:
        reference_keyed_derivation().derive_batch(
            array, mask_key=_MASK_KEY, namespace="", truncate=None
        )
    assert ref_exc.value.code == "namespace_empty"

    with pytest.raises(DeterminismError) as loaded_exc:
        kernel.derive_batch(array, mask_key=_MASK_KEY, namespace="", truncate=None)
    assert loaded_exc.value.code == "namespace_empty"


@pytestmark_present
def test_float_array_raises_same_type_as_reference() -> None:
    # The reference raises GenerationError(code="float_canonicalization_unsupported")
    # per non-null float value; the compiled kernel rejects the whole array up front
    # with one coded ValueError, translated here to the same GenerationError TYPE
    # (not necessarily the reference's more specific code -- see
    # `_translate_compiled_kernel_error`'s docstring).
    kernel = load_compiled_crypto_kernel()
    array = pa.array([1.0, 2.0], type=pa.float64())

    with pytest.raises(GenerationError) as ref_exc:
        reference_keyed_derivation().derive_batch(
            array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None
        )
    assert ref_exc.value.code == "float_canonicalization_unsupported"

    with pytest.raises(GenerationError):
        kernel.derive_batch(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None)


@pytestmark_present
def test_naive_timestamp_array_raises_same_type_as_reference() -> None:
    import datetime

    kernel = load_compiled_crypto_kernel()
    array = pa.array([datetime.datetime(2020, 1, 1)], type=pa.timestamp("us", tz=None))

    with pytest.raises(GenerationError) as ref_exc:
        reference_keyed_derivation().derive_batch(
            array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None
        )
    assert ref_exc.value.code == "timezone_naive_datetime"

    with pytest.raises(GenerationError):
        kernel.derive_batch(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None)


@pytestmark_present
def test_wrong_length_key_with_a_non_null_row_produces_no_output() -> None:
    # Fail-before-output: a batch that reaches a non-null row under a wrong-length
    # key must raise, never return a partially-derived array.
    kernel = load_compiled_crypto_kernel()
    array = pa.array([None, "alice", None])

    with pytest.raises(DeterminismError):
        kernel.derive_batch(array, mask_key=bytes(20), namespace=_NAMESPACE, truncate=None)

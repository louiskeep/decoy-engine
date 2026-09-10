"""Tests for `load_compiled_index_kernel` (Task 2.3 Phase 1): the loader, the shared
ABI check, capability detection of the additive `derive_index_batch` symbol, and the
fail-before-output contract, mirroring `test_crypto_ext_loader.py`'s structure.

Three groups:

- Loader-mechanics tests (missing symbol, wrong values, wrong ABI) inject a stand-in
  module tree via `sys.modules` so they run identically whether or not the real
  `decoy-engine-native` companion happens to be installed, and assert the STAGE that
  failed (missing-symbol vs self-test-failure produce distinct messages, per D2).
- Kernel-behavior tests (KAT parity, differential-vs-reference, error mapping) need
  the real compiled kernel and are skipped when the companion is not installed.
- Embedding-drift + reference-only tests need no companion at all.
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.determinism import DeterminismError, derive_index
from decoy_engine.errors import MaskKeyRequiredError
from decoy_engine.execution.native._index_ext import (
    INDEX_KAT,
    CryptoExtensionUnavailableError,
    _translate_compiled_index_kernel_error,
    load_compiled_index_kernel,
    reference_index_derivation,
)
from decoy_engine.generation.pool._canonicalize import _canonicalize_source

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "decoy-engine-native"
    / "vectors"
    / "derive_index_kat.json"
)


# ---------------------------------------------------------------------------
# Embedding-drift: the module-level INDEX_KAT constant must match what the live
# reference primitives produce for the same inputs, so a future edit to
# `derive_index` / `_canonicalize_source` (or a typo in the embedded constant)
# is caught here, without any runtime dependency on the repo's JSON fixture.
# ---------------------------------------------------------------------------


def test_embedded_kat_matches_the_live_python_reference() -> None:
    expected = tuple(
        None
        if value is None
        else derive_index(
            INDEX_KAT.mask_key,
            INDEX_KAT.namespace,
            _canonicalize_source(value),
            pool_size=INDEX_KAT.pool_size,
        )
        for value in INDEX_KAT.values
    )
    assert expected == INDEX_KAT.expected


def test_embedded_kat_matches_reference_index_derivation_wrapper() -> None:
    # Same check through the Protocol-shaped oracle rather than the raw primitives,
    # so a drift in `_ReferenceIndexDerivation`'s own null handling is also caught.
    out = reference_index_derivation().derive_index_batch(
        pa.array(INDEX_KAT.values, type=pa.string()),
        mask_key=INDEX_KAT.mask_key,
        namespace=INDEX_KAT.namespace,
        pool_size=INDEX_KAT.pool_size,
    )
    assert out.type == pa.uint64()
    assert tuple(out.to_pylist()) == INDEX_KAT.expected


# ---------------------------------------------------------------------------
# `reference_index_derivation` works with no companion involved at all.
# ---------------------------------------------------------------------------


def test_reference_index_derivation_is_pure_python_and_null_safe() -> None:
    ref = reference_index_derivation()
    out = ref.derive_index_batch(
        pa.array(["alice", None, "bob"], type=pa.string()),
        mask_key=bytes(range(32)),
        namespace="pool.city",
        pool_size=1000,
    )
    assert out.type == pa.uint64()
    values = out.to_pylist()
    assert values[1] is None
    assert all(isinstance(v, int) for v in (values[0], values[2]))


def test_reference_index_derivation_requires_mask_key() -> None:
    ref = reference_index_derivation()
    with pytest.raises(MaskKeyRequiredError):
        ref.derive_index_batch(
            pa.array(["alice"], type=pa.string()),
            mask_key=None,
            namespace="pool.city",
            pool_size=1000,
        )


# ---------------------------------------------------------------------------
# `_translate_compiled_index_kernel_error`: exercised directly, independent of
# the real compiled companion (a pure function over a ValueError's message).
# ---------------------------------------------------------------------------


def test_translate_pool_size_invalid_maps_to_determinism_error() -> None:
    exc = ValueError("pool_size_invalid: pool_size must be >= 1; got 0")
    translated = _translate_compiled_index_kernel_error(exc)
    assert type(translated) is DeterminismError
    assert translated.code == "pool_size_invalid"


def test_translate_pool_size_overflow_maps_to_determinism_error() -> None:
    exc = ValueError("pool_size_overflow: pool_size 999999999999999999 exceeds maximum")
    translated = _translate_compiled_index_kernel_error(exc)
    assert type(translated) is DeterminismError
    assert translated.code == "pool_size_overflow"


def test_translate_passes_through_an_unrecognized_code_unchanged() -> None:
    exc = ValueError("some_future_code: detail text")
    translated = _translate_compiled_index_kernel_error(exc)
    assert translated is exc


# ---------------------------------------------------------------------------
# Loader mechanics: fake `sys.modules` injection, independent of the real companion.
# ---------------------------------------------------------------------------

_NO_DERIVE_INDEX_BATCH = object()


def _install_fake_kernel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    abi: str,
    derive_index_batch: Callable[..., pa.Array] | object | None = None,
) -> None:
    """Inject a stand-in `decoy_engine_native._kernel` exposing only `abi_version`
    plus (optionally) `derive_index_batch`, mirroring the crypto loader test's
    `_install_fake_kernel`. `derive_index_batch=None` omits the attribute entirely
    (the missing-symbol path); any other value is installed verbatim."""

    fake_kernel = types.ModuleType("decoy_engine_native._kernel")
    fake_kernel.abi_version = lambda: abi  # type: ignore[attr-defined]
    if derive_index_batch is not None:
        fake_kernel.derive_index_batch = derive_index_batch  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("decoy_engine_native")
    fake_pkg._kernel = fake_kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", fake_pkg)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", fake_kernel)


def test_absent_companion_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "decoy_engine_native", None)
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_index_kernel()


def test_wrong_abi_tag_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-0-stale")
    with pytest.raises(CryptoExtensionUnavailableError):
        load_compiled_index_kernel()


def test_missing_derive_index_batch_symbol_fails_at_the_missing_symbol_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abi-2 companion that predates the index kernel (Task 1.6 era) has no
    `derive_index_batch` attribute at all. This must fail BEFORE the self-test runs,
    with a message naming the missing symbol -- not the self-test-failure message a
    later branch would raise for a present-but-wrong entry point."""
    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_index_batch=None)
    with pytest.raises(CryptoExtensionUnavailableError) as exc_info:
        load_compiled_index_kernel()
    message = str(exc_info.value)
    assert "lacks derive_index_batch" in message
    assert "self-test" not in message


def test_present_but_wrong_values_fails_at_the_self_test_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `derive_index_batch` that exists and is callable but does not reproduce the
    embedded KAT fails at the SELF-TEST stage, with a message distinct from the
    missing-symbol one (round-2 plan-gate requirement: assert the STAGE, not merely
    that loading failed)."""

    def _wrong(values: pa.Array, **kwargs: object) -> pa.Array:
        return pa.array([0] * len(values), type=pa.uint64())

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_index_batch=_wrong)
    with pytest.raises(CryptoExtensionUnavailableError) as exc_info:
        load_compiled_index_kernel()
    message = str(exc_info.value)
    assert "self-test" in message
    assert "lacks derive_index_batch" not in message


def test_present_but_raising_fails_at_the_self_test_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present `derive_index_batch` that raises during the self-test call is the
    same failure STAGE as one that returns wrong values (both happen after the
    symbol resolved cleanly), so it must carry the same self-test message, not the
    missing-symbol one."""

    def _raises(values: pa.Array, **kwargs: object) -> pa.Array:
        raise RuntimeError("simulated companion failure")

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_index_batch=_raises)
    with pytest.raises(CryptoExtensionUnavailableError) as exc_info:
        load_compiled_index_kernel()
    message = str(exc_info.value)
    assert "self-test" in message
    assert "lacks derive_index_batch" not in message


def test_present_but_wrong_arrow_type_fails_the_self_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Right Python values, wrong Arrow type: an entry point returning int64 where the
    # reference returns uint64 must be rejected, so a type mismatch cannot ride in on
    # a matching .to_pylist().
    def _wrong_type(values: pa.Array, **kwargs: object) -> pa.Array:
        return pa.array(list(INDEX_KAT.expected), type=pa.int64())

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_index_batch=_wrong_type)
    with pytest.raises(CryptoExtensionUnavailableError) as exc_info:
        load_compiled_index_kernel()
    assert "self-test" in str(exc_info.value)


def test_matching_abi_and_kat_returns_a_working_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = reference_index_derivation()

    def _recording(values: pa.Array, **kwargs: object) -> pa.Array:
        return reference.derive_index_batch(values, **kwargs)  # type: ignore[arg-type]

    _install_fake_kernel(monkeypatch, abi="decoy-native-abi-2", derive_index_batch=_recording)
    kernel = load_compiled_index_kernel()
    out = kernel.derive_index_batch(
        pa.array(["alice", "bob"], type=pa.string()),
        mask_key=bytes(range(32)),
        namespace="pool.city",
        pool_size=1000,
    )
    assert out.type == pa.uint64()
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Kernel behavior: needs the real compiled companion.
# ---------------------------------------------------------------------------


def _fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


@_NEEDS_COMPANION
def test_loader_succeeds_against_the_real_companion() -> None:
    kernel = load_compiled_index_kernel()
    out = kernel.derive_index_batch(
        pa.array(["alice", "bob", None], type=pa.string()),
        mask_key=bytes(range(32)),
        namespace="pool.city",
        pool_size=1000,
    )
    assert out.type == pa.uint64()
    assert out.to_pylist()[2] is None


@_NEEDS_COMPANION
def test_loaded_kernel_reproduces_a_couple_of_string_fixture_cases() -> None:
    """Load a couple of `utf8`-kind cases from the repo's derive_index_kat.json
    fixture directly in the test (permitted here, unlike the load-time self-test,
    which must not depend on a repo asset) and check the loaded wrapper against them."""
    kernel = load_compiled_index_kernel()
    string_cases = [
        c for c in _fixture()["cases"] if c["arrow_type"]["kind"] in ("utf8", "large_utf8")
    ][:2]
    assert string_cases, "expected at least one utf8/large_utf8 case in the fixture"
    for case in string_cases:
        dtype = pa.string() if case["arrow_type"]["kind"] == "utf8" else pa.large_string()
        array = pa.array(case["logical_values"], type=dtype)
        out = kernel.derive_index_batch(
            array,
            mask_key=bytes.fromhex(case["seed_hex"]),
            namespace=case["namespace"],
            pool_size=case["pool_size"],
            native_threads=1,
        )
        assert out.type == pa.uint64()
        assert out.to_pylist() == case["expected_index"], case["name"]


@_NEEDS_COMPANION
@pytest.mark.parametrize("native_threads", [1, 2, 4])
def test_loaded_kernel_matches_reference_over_random_inputs(native_threads: int) -> None:
    kernel = load_compiled_index_kernel()
    reference = reference_index_derivation()
    rng = random.Random(20260910)
    values = [None if rng.random() < 0.2 else f"user-{i}" for i in range(200)]
    array = pa.array(values, type=pa.string())
    mask_key = bytes(range(32))

    for pool_size in (1, 97, 100003):
        loaded_out = kernel.derive_index_batch(
            array,
            mask_key=mask_key,
            namespace="pool.city",
            pool_size=pool_size,
            native_threads=native_threads,
        )
        reference_out = reference.derive_index_batch(
            array, mask_key=mask_key, namespace="pool.city", pool_size=pool_size
        )
        assert loaded_out.type == pa.uint64()
        assert loaded_out.to_pylist() == reference_out.to_pylist()


@_NEEDS_COMPANION
def test_loaded_kernel_matches_reference_over_a_chunked_array() -> None:
    kernel = load_compiled_index_kernel()
    reference = reference_index_derivation()
    chunked = pa.chunked_array([["alice", "bob"], [None, "carol"]])
    mask_key = bytes(range(32))

    loaded_out = kernel.derive_index_batch(
        chunked, mask_key=mask_key, namespace="pool.city", pool_size=1000
    )
    reference_out = reference.derive_index_batch(
        chunked, mask_key=mask_key, namespace="pool.city", pool_size=1000
    )
    assert loaded_out.to_pylist() == reference_out.to_pylist()


@_NEEDS_COMPANION
def test_coded_pool_size_error_maps_to_determinism_error() -> None:
    kernel = load_compiled_index_kernel()
    array = pa.array(["alice"], type=pa.string())
    mask_key = bytes(range(32))

    with pytest.raises(DeterminismError) as exc_info:
        kernel.derive_index_batch(array, mask_key=mask_key, namespace="pool.city", pool_size=0)
    assert exc_info.value.code == "pool_size_invalid"

    with pytest.raises(DeterminismError) as exc_info:
        kernel.derive_index_batch(
            array, mask_key=mask_key, namespace="pool.city", pool_size=1 << 57
        )
    assert exc_info.value.code == "pool_size_overflow"


@_NEEDS_COMPANION
def test_non_int_pool_size_raises_type_error() -> None:
    kernel = load_compiled_index_kernel()
    array = pa.array(["alice", "bob"], type=pa.string())
    mask_key = bytes(range(32))

    with pytest.raises(TypeError):
        kernel.derive_index_batch(
            array, mask_key=mask_key, namespace="pool.city", pool_size="not-an-int"
        )


@_NEEDS_COMPANION
def test_missing_mask_key_raises_mask_key_required_before_output() -> None:
    kernel = load_compiled_index_kernel()
    array = pa.array(["alice"], type=pa.string())

    with pytest.raises(MaskKeyRequiredError):
        kernel.derive_index_batch(array, mask_key=None, namespace="pool.city", pool_size=1000)

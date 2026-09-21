"""Compiled raw-hex-kernel loader + pure-Python oracle for the group_key derivation.

The native `group_key` operator needs a derivation the existing kernels cannot
provide byte-for-byte: the oracle (`transforms/group_key.py`) hashes the RAW
bytes of `str(value)` with NO canonicalizer, while `derive_batch` canonicalizes
each row inside Rust (NFC strings, length-prefix ints, special-encode
bool/date). So the companion ships a SEPARATE entry point,
`derive_hex_raw_batch`, that skips canonicalization and hashes the raw utf8
bytes of an already-stringified `pa.string()` column.

`derive_hex_raw_batch(values, *, mask_key, namespace, hex_chars, native_threads)
-> pa.Array[string]` returns `derive(mask_key, namespace, value.utf8_bytes)
[:hex_chars//2].hex()` per row. `hex_chars` is the config `length` (even, in
`[8, 64]`); the caller stringifies the sibling column with pandas
`Series.astype(str)` before the call, so a null cell arrives as the string
"None", never a null.

Loader shape mirrors `_index_ext.load_compiled_index_kernel`: import, ABI
check, capability-detect the additive symbol, run a load-time known-answer
self-test, then a thin wrapper. It lives in its own module (not `_crypto_ext`)
because the raw-hex kernel is an ADDITIVE symbol on the existing abi-2
companion (like `derive_index_batch`, `_index_ext` docstring / D2), never a
reason to bump the shared ABI tag: an older companion that predates the raw
symbol reports the right ABI and passes the hash/index self-tests while lacking
`derive_hex_raw_batch` entirely, which this loader detects as a clean,
distinctly-coded absence so group_key declines to the oracle while hash-only
tables keep native acceleration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import pyarrow as pa

from decoy_engine.determinism import DeterminismError
from decoy_engine.determinism._derive import derive
from decoy_engine.generation.pool._errors import GenerationError

from ._crypto_ext import (
    _EXPECTED_ABI_VERSION,
    CryptoExtensionUnavailableError,
    KernelInput,
    _require_mask_key,
)

# ---------------------------------------------------------------------------
# Embedded known-answer vector.
#
# `expected` was computed at BUILD TIME from the shipped Python `derive`
# primitive (the same one `_ReferenceRawHexDerivation` below calls);
# `tests/native/test_group_key_ext.py` re-derives it from that primitive so a
# future edit to either side surfaces as embedded-constant drift, never silently
# trusted. Values are already-stringified (the kernel never sees a raw non-string
# value): the stringification parity is proven separately at the operator level.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupKeyRawKatVector:
    mask_key: bytes
    namespace: str
    hex_chars: int
    values: tuple[str, ...]
    expected: tuple[str, ...]


RAW_HEX_KAT = GroupKeyRawKatVector(
    mask_key=bytes(range(32)),
    namespace="group_key/household_id",
    hex_chars=16,
    values=("alice", "bob", "alice", "carol"),
    expected=(
        "ba38a11936bfef1a",
        "5a27b088b50251d3",
        "ba38a11936bfef1a",
        "24de696ee019b77e",
    ),
)


# ---------------------------------------------------------------------------
# Kernel protocol + error translation.
# ---------------------------------------------------------------------------


class RawHexDerivationKernel(Protocol):
    """Canonicalize-free keyed hex derivation as a batch kernel over one column."""

    def derive_hex_raw_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        hex_chars: int,
        native_threads: int | None = None,
    ) -> pa.Array: ...


def _translate_compiled_raw_hex_kernel_error(exc: ValueError) -> Exception:
    """Map one of the compiled kernel's coded `ValueError`s onto the exact
    exception type the reference raises for the same input (the drop-in-wrapper
    contract `_crypto_ext._translate_compiled_kernel_error` establishes for the
    hash kernel). `mask_key_required` never reaches here: the wrapper checks it
    up front via `_require_mask_key`, matching the reference's own unconditional
    pre-loop guard. The two group_key-specific codes
    (`group_key_input_not_string`, `group_key_hex_chars_invalid`) are DEFENSIVE:
    admission guarantees a stringified column and a valid even `hex_chars`, so
    they are wiring faults, surfaced as `GenerationError` rather than silently
    swallowed."""
    code, _, detail = str(exc).partition(": ")
    if code in ("seed_wrong_length", "namespace_empty"):
        return DeterminismError(code=code, message=detail)
    if code in (
        "mixed_object_not_native",
        "group_key_input_not_string",
        "group_key_hex_chars_invalid",
    ):
        return GenerationError(code=code, message=detail)
    return exc


class _CompiledRawHexKernel:
    """Thin wrapper around the compiled `decoy_engine_native._kernel.
    derive_hex_raw_batch`, satisfying `RawHexDerivationKernel` and behaving as a
    drop-in for `_ReferenceRawHexDerivation` over `pa.Array` input: same output
    bytes, and the same exception types for every input the reference rejects."""

    def __init__(self, derive_hex_raw_batch_fn: Callable[..., pa.Array]) -> None:
        self._derive_hex_raw_batch_fn = derive_hex_raw_batch_fn

    def derive_hex_raw_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        hex_chars: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        # Fail before the compiled kernel is even called, matching the
        # reference's unconditional pre-loop guard exactly.
        key = _require_mask_key(mask_key, "group_key_derivation")
        array = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
        try:
            return self._derive_hex_raw_batch_fn(
                array,
                mask_key=key,
                namespace=namespace,
                hex_chars=hex_chars,
                native_threads=native_threads,
            )
        except ValueError as exc:
            raise _translate_compiled_raw_hex_kernel_error(exc) from exc


# ---------------------------------------------------------------------------
# Loader: import, ABI check, capability-detect, load-time KAT self-test.
# ---------------------------------------------------------------------------


def load_compiled_raw_hex_kernel() -> RawHexDerivationKernel:
    """Load the compiled raw-hex kernel, or fail before any output.

    Imports `decoy_engine_native._kernel`, checks its `abi_version()` against
    the SHARED `_EXPECTED_ABI_VERSION` `_crypto_ext` pins (no ABI bump for this
    additive symbol), capability-detects `derive_hex_raw_batch`, and runs the
    `RAW_HEX_KAT` vector through it. Every failure mode raises
    `CryptoExtensionUnavailableError` BEFORE returning, with a message that
    distinguishes the STAGE that failed -- mirroring `load_compiled_index_kernel`:

    - missing companion / import failure / ABI mismatch: same staged messages as
      the crypto/index loaders (an incompatible companion is incompatible for
      every kernel it offers).
    - missing `derive_hex_raw_batch` symbol: a companion built before the raw
      kernel existed (still-valid abi-2) reports this DISTINCT message so a
      caller/test can tell "too old for group_key" apart from "built wrong".
    - the self-test raises or returns a wrong-shaped/wrong-valued result: a
      third, distinct message.

    An abi-2 companion lacking the raw symbol fails THIS loader while the hash
    loader still succeeds, so hash-only tables keep native acceleration and
    group_key tables downgrade to the oracle (the whole-table decline policy)."""
    try:
        from decoy_engine_native import _kernel
    except Exception as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion is not installed or failed to load; "
            "install it directly (no 'native' extra exists yet, see "
            "docs/native/supported-matrix.md), or use the pure-Python reference kernel "
            "(reference_raw_hex_derivation) instead."
        ) from exc

    try:
        reported_abi = _kernel.abi_version()
    except Exception as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion's abi_version() call failed; "
            "treating it as incompatible rather than risking a stale binary."
        ) from exc

    if reported_abi != _EXPECTED_ABI_VERSION:
        raise CryptoExtensionUnavailableError(
            f"the decoy-engine-native companion reports ABI {reported_abi!r}, "
            f"expected {_EXPECTED_ABI_VERSION!r}; refusing to run against a stale "
            "or incompatible binary."
        )

    # A matching ABI tag says the companion was built against this core revision,
    # not that it carries the raw-hex kernel: `derive_hex_raw_batch` is an
    # additive symbol, so an old abi-2 companion legitimately lacks it. Resolve
    # the attribute in its own guarded step so that specific, coded absence is
    # never confused with a self-test failure below.
    try:
        derive_hex_raw_batch_fn = _kernel.derive_hex_raw_batch
    except AttributeError as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion lacks derive_hex_raw_batch; it "
            "predates the group_key raw-hex kernel (still a valid abi-2 build for "
            "the keyed-hash route). group_key tables downgrade to the oracle; "
            "hash-only tables keep native acceleration."
        ) from exc

    # Belt-and-suspenders with the attribute check: `callable()` can lie for a
    # `__call__ = None` object, and merely reading a pathological descriptor can
    # raise. Running one known-answer vector HERE turns every remaining malformed
    # shape -- non-callable, a raising descriptor, or (the realistic one) a
    # mis-built binary that derives wrong bytes -- into a single fail-closed load
    # error, mirroring the crypto/index loaders' self-test. The Arrow type is
    # checked before `.to_pylist()` so a wrong-typed result cannot slip past on a
    # coincidentally-matching value list.
    try:
        probe_out = derive_hex_raw_batch_fn(
            pa.array(RAW_HEX_KAT.values, type=pa.string()),
            mask_key=RAW_HEX_KAT.mask_key,
            namespace=RAW_HEX_KAT.namespace,
            hex_chars=RAW_HEX_KAT.hex_chars,
            native_threads=1,
        )
        probe_reproduces_reference = (
            isinstance(probe_out, pa.Array)
            and probe_out.type == pa.string()
            and probe_out.to_pylist() == list(RAW_HEX_KAT.expected)
        )
    except Exception as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion's 'derive_hex_raw_batch' entry point "
            "raised during the load-time known-answer self-test; treating it as "
            "incompatible rather than returning a half-initialized kernel that "
            "would fail mid-derive."
        ) from exc

    if not probe_reproduces_reference:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion's derive_hex_raw_batch failed its "
            "load-time known-answer self-test; refusing to run a binary that "
            "does not reproduce the reference raw-hex derivation."
        )

    return _CompiledRawHexKernel(derive_hex_raw_batch_fn)


# ---------------------------------------------------------------------------
# Pure-Python reference (the differential oracle).
# ---------------------------------------------------------------------------


class _ReferenceRawHexDerivation:
    """Pure-Python reference for the canonicalize-free hex derivation.

    Operates on an ALREADY-STRINGIFIED `pa.string()` column, matching the
    compiled kernel's contract: `derive(mask_key, namespace, s.encode())
    [:hex_chars//2].hex()` per row, no canonicalization. Null in -> null out
    (a defensive carry-through; the group_key operator's `astype(str)` leaves no
    nulls). This is the differential oracle the compiled kernel is graded
    against, and reuses the shipped `derive` so output is byte-identical by
    construction."""

    def derive_hex_raw_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        hex_chars: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        # `native_threads` is accepted for Protocol conformance but IGNORED: the
        # compiled kernel is thread-invariant, so the single-threaded reference
        # is a sound parity oracle at every thread count.
        del native_threads
        key = _require_mask_key(mask_key, "group_key_derivation")
        n_bytes = hex_chars // 2
        array = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
        out: list[str | None] = []
        for value in array.to_pylist():
            if value is None:
                out.append(None)
                continue
            out.append(derive(key, namespace, str(value).encode("utf-8"))[:n_bytes].hex())
        return pa.array(out, type=pa.string())


def reference_raw_hex_derivation() -> RawHexDerivationKernel:
    """Return the pure-Python raw-hex-derivation reference kernel."""
    return _ReferenceRawHexDerivation()


__all__ = [
    "RAW_HEX_KAT",
    "CryptoExtensionUnavailableError",
    "GroupKeyRawKatVector",
    "RawHexDerivationKernel",
    "load_compiled_raw_hex_kernel",
    "reference_raw_hex_derivation",
]

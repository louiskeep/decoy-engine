"""Compiled index-kernel loader + pure-Python oracle for deterministic Faker selection.

Task 2.3 (native-throughput consolidation, Phase 1) wires the compiled
`derive_index_batch` kernel (Task 2.2) into the engine the same way
`_crypto_ext.load_compiled_crypto_kernel` wires `derive_batch`: import, ABI
check, a load-time known-answer self-test, then a thin wrapper. It lives in
its own module rather than `_crypto_ext.py` (round-3 plan-gate finding): the
index kernel is an ADDITIVE capability on the existing abi-2 companion (see
`_EXPECTED_ABI_VERSION` in `_crypto_ext`), never a reason to bump the shared
ABI tag or grow that module past its size cap.

`derive_index_batch(values, *, mask_key, namespace, pool_size, native_threads)
-> pa.Array[uint64]` returns one deterministic pool index per row (null in ->
null out), matching the frozen `derive_index` contract
(`docs/native/derive-index-contract.md`; see `determinism.derive_index` and
`generation.pool._canonicalize._canonicalize_source`). Loading and self-testing
happen ONCE, at preflight (this module never re-derives per chunk); threading
the verified wrapper through the masking route is Phase 2/3 of Task 2.3, not
this module's job.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import pyarrow as pa

from decoy_engine.determinism import DeterminismError, derive_index
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.kernel._scalar import _array_to_pylist, _is_missing

from ._crypto_ext import (
    _EXPECTED_ABI_VERSION,
    CryptoExtensionUnavailableError,
    KernelInput,
    _require_mask_key,
)

# ---------------------------------------------------------------------------
# Embedded known-answer vector.
#
# NOT read from decoy-engine-native/vectors/derive_index_kat.json at runtime:
# that fixture is a repo asset, not part of the installed wheel, so the
# load-time self-test needs its own pinned constant. `expected` was computed
# at BUILD TIME with the shipped Python reference (`derive_index` composed
# with `_canonicalize_source`, the same primitives `_ReferenceIndexDerivation`
# below calls); `tests/native/test_index_ext.py` re-derives it from those same
# primitives so a future edit to either one is caught as embedded-constant
# drift, not silently trusted.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexKatVector:
    mask_key: bytes
    namespace: str
    pool_size: int
    values: tuple[str | None, ...]
    expected: tuple[int | None, ...]


INDEX_KAT = IndexKatVector(
    mask_key=bytes(range(32)),
    namespace="pool.city",
    pool_size=97,
    values=("alice", "bob", None, "carol"),
    expected=(59, 61, None, 90),
)


# ---------------------------------------------------------------------------
# Kernel protocol + error translation.
# ---------------------------------------------------------------------------


class IndexDerivationKernel(Protocol):
    """Deterministic pool-index selection as a batch kernel over one column."""

    def derive_index_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> pa.Array: ...


def _translate_compiled_index_kernel_error(exc: ValueError) -> Exception:
    """Map one of the compiled kernel's coded `ValueError`s onto the exact exception
    type `_ReferenceIndexDerivation.derive_index_batch` raises for the same input
    (the drop-in-wrapper contract `_crypto_ext._translate_compiled_kernel_error`
    already establishes for the hash kernel; index adds two pool-size codes the hash
    kernel never raises). `mask_key_required` never reaches here:
    `_CompiledIndexKernel.derive_index_batch` checks that up front via
    `_require_mask_key`, matching the reference's own unconditional pre-loop guard."""
    code, _, detail = str(exc).partition(": ")
    if code in ("seed_wrong_length", "namespace_empty", "pool_size_invalid", "pool_size_overflow"):
        return DeterminismError(code=code, message=detail)
    if code == "mixed_object_not_native":
        # Same fold as the hash kernel: the compiled kernel rejects every unadmitted
        # Arrow type with one coded ValueError, where the reference's canonicalizer
        # raises a finer-grained GenerationError code per value type. The exception
        # TYPE is the drop-in guarantee, not the specific code.
        return GenerationError(code="native_type_not_admitted", message=detail)
    return exc


class _CompiledIndexKernel:
    """Thin wrapper around the compiled `decoy_engine_native._kernel.derive_index_batch`,
    satisfying `IndexDerivationKernel` and behaving as a drop-in for
    `_ReferenceIndexDerivation` over `pa.Array` input: same output values, and the
    same exception types for every input the reference also rejects."""

    def __init__(self, derive_index_batch_fn: Callable[..., pa.Array]) -> None:
        self._derive_index_batch_fn = derive_index_batch_fn

    def derive_index_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        # Fail before the compiled kernel is even called, matching
        # `_ReferenceIndexDerivation.derive_index_batch`'s unconditional pre-loop
        # guard exactly (same message, same exception type).
        key = _require_mask_key(mask_key, "index_derivation")
        # A ChunkedArray is still one Arrow-typed column in more than one buffer;
        # combine to the single pa.Array the compiled entry point accepts.
        array = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
        try:
            return self._derive_index_batch_fn(
                array,
                mask_key=key,
                namespace=namespace,
                pool_size=pool_size,
                native_threads=native_threads,
            )
        except ValueError as exc:
            raise _translate_compiled_index_kernel_error(exc) from exc


# ---------------------------------------------------------------------------
# Loader: import, ABI check, capability-detect, load-time KAT self-test.
# ---------------------------------------------------------------------------


def load_compiled_index_kernel() -> IndexDerivationKernel:
    """Load the compiled index kernel, or fail before any output.

    Imports `decoy_engine_native._kernel`, checks its `abi_version()` against the
    SHARED `_EXPECTED_ABI_VERSION` `_crypto_ext` pins (no ABI bump for this
    additive symbol -- D2), capability-detects `derive_index_batch`, and runs one
    known-answer vector (`INDEX_KAT`) through it. Every failure mode raises
    `CryptoExtensionUnavailableError` BEFORE returning, with a message that
    distinguishes the STAGE that failed:

    - missing companion / import failure / ABI mismatch: same staged messages as
      `load_compiled_crypto_kernel` (an incompatible companion is incompatible
      for every kernel it might otherwise offer).
    - missing `derive_index_batch` symbol: a companion built before the index
      kernel existed (still-valid abi-2, Task 1.6 era) reports this DISTINCT
      message so a caller/test can tell "too old for indexing" apart from "built
      wrong".
    - the self-test raises, or returns a wrong-shaped/wrong-valued result: a
      third, again distinct, message -- the symbol exists but does not
      reproduce the reference.

    A caller must not treat this as interchangeable with `CryptoExtensionUnavailableError`
    from the hash loader: an index-less abi-2 companion fails THIS loader while the hash
    loader still succeeds, so hash-only tables keep native acceleration and
    faker/mixed tables downgrade to the oracle (the D1 whole-table policy)."""
    try:
        from decoy_engine_native import _kernel
    except Exception as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion is not installed or failed to load; "
            "install it directly (no 'native' extra exists yet, see "
            "docs/native/supported-matrix.md), or use the pure-Python reference kernel "
            "(reference_index_derivation) instead."
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
    # not that it carries the index kernel: `derive_index_batch` is an additive
    # symbol (D2), so an old abi-2 companion legitimately lacks it. Resolve the
    # attribute in its own guarded step so that specific, coded absence is never
    # confused with a self-test failure below.
    try:
        derive_index_batch_fn = _kernel.derive_index_batch
    except AttributeError as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion lacks derive_index_batch; it "
            "predates the index kernel (still a valid abi-2 build for the "
            "keyed-hash route). Faker/index-selection tables downgrade to the "
            "oracle; hash-only tables keep native acceleration."
        ) from exc

    # Belt-and-suspenders with the attribute check above: `callable()` can lie for
    # a `__call__ = None` object, and merely reading a pathological descriptor can
    # raise. Running one known-answer vector through the entry point HERE, at
    # load, turns every remaining malformed shape -- non-callable, a raising
    # descriptor, or (the realistic one) a mis-built binary that derives wrong
    # indices -- into a single fail-closed load error, mirroring the crypto
    # loader's self-test.
    try:
        probe_out = derive_index_batch_fn(
            pa.array(INDEX_KAT.values, type=pa.string()),
            mask_key=INDEX_KAT.mask_key,
            namespace=INDEX_KAT.namespace,
            pool_size=INDEX_KAT.pool_size,
            native_threads=1,
        )
        # Require the exact Arrow type the reference emits, not just matching
        # Python values, so a wrong-width integer array cannot slip past the
        # load gate on a coincidentally-matching `.to_pylist()`.
        probe_reproduces_reference = (
            isinstance(probe_out, pa.Array)
            and probe_out.type == pa.uint64()
            and probe_out.to_pylist() == list(INDEX_KAT.expected)
        )
    except Exception as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion's 'derive_index_batch' entry point "
            "raised during the load-time known-answer self-test; treating it as "
            "incompatible rather than returning a half-initialized kernel that "
            "would fail mid-derive."
        ) from exc

    if not probe_reproduces_reference:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion's derive_index_batch failed its "
            "load-time known-answer self-test; refusing to run a binary that "
            "does not reproduce the reference index derivation."
        )

    return _CompiledIndexKernel(derive_index_batch_fn)


# ---------------------------------------------------------------------------
# Pure-Python reference (the differential oracle).
# ---------------------------------------------------------------------------


class _ReferenceIndexDerivation:
    """Pure-Python reference for deterministic pool-index selection.

    Reuses the shipped normalization (`_array_to_pylist`), null policy
    (`_is_missing`), canonicalizer, and `derive_index`, so output is
    byte-identical by construction while carrying the native contract's
    mask_key naming and fail-closed guard."""

    def derive_index_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        # `native_threads` is accepted for Protocol conformance but IGNORED: this
        # single-threaded Python reference is the parity oracle, and the compiled
        # kernel's output is thread-invariant (proven at every thread count), so
        # the thread count never changes indices on either side.
        del native_threads
        key = _require_mask_key(mask_key, "index_derivation")
        out: list[int | None] = []
        for value in _array_to_pylist(values):
            if _is_missing(value):
                out.append(None)
                continue
            out.append(
                derive_index(key, namespace, _canonicalize_source(value), pool_size=pool_size)
            )
        return pa.array(out, type=pa.uint64())


def reference_index_derivation() -> IndexDerivationKernel:
    """Return the pure-Python index-derivation reference kernel."""
    return _ReferenceIndexDerivation()


__all__ = [
    "INDEX_KAT",
    "CryptoExtensionUnavailableError",
    "IndexDerivationKernel",
    "IndexKatVector",
    "load_compiled_index_kernel",
    "reference_index_derivation",
]

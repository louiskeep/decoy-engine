"""Byte-parity regression for the engine-side native-mask thread-knee clamp.

The clamp lives in one place, the Rust thread-budget resolver
(`decoy-engine-native/src/threads.rs::NativeThreadBudget::resolve`): a per-call
`native_threads` request is lowered to `min(requested, host_available, knee)` so no caller can
exceed the measured knee. Its correctness contract is that it changes SPEED, never OUTPUT: a
native masking job derives each row independently and arbitrates errors by lowest row index, so
the worker-thread count cannot affect the bytes.

There is no Python-observable accessor for the resolved budget (the compiled kernel exposes only
`abi_version`, `derive_batch`, `derive_index_batch`), so the clamp's effect cannot be asserted
directly from Python. The observable, and required, regression is byte-identity across thread
counts: a request of 8 that the resolver clamps to the default knee of 4 must produce output that
is bit-identical (value AND Arrow field type) to a request of 1 or 4. This exercises both shared
callers of the resolver, `derive_batch` (hash) and `derive_index_batch` (faker/index), which is
the whole surface the PR-1a clamp touches.
"""

from __future__ import annotations

import importlib.util

import pytest

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)

# One representative column: nulls (incl. leading/trailing), duplicates, empty string, ASCII and
# multibyte unicode, and lengths that straddle any internal slice boundary. Kept well above a
# single row so a multi-thread grant actually fans the array into more than one range.
_VALUES = (
    [None, "alice", "bob", "", "alice", "Ada Lovelace", "éèê", "\U0001f600ab"]
    + [None if i % 7 == 0 else f"user-{i}-{'z' * (i % 5)}" for i in range(200)]
    + [None]
)

# The clamp's own arithmetic: on any host with >= 4 cores, a request of 8 is granted the default
# knee (4); 1 and 4 pass through. The three must be byte-identical regardless.
_THREAD_COUNTS = (1, 4, 8)

_MASK_KEY = bytes(range(32))


def _string_array():
    import pyarrow as pa

    return pa.array(_VALUES, type=pa.string())


@_NEEDS_COMPANION
def test_hash_kernel_is_byte_identical_across_thread_counts() -> None:
    """derive_batch (hash) output is bit-identical for native_threads in {1, 4, 8}: the clamp
    lowers an 8-thread request to the knee but never perturbs the bytes."""
    import decoy_engine_native._kernel as kernel

    array = _string_array()
    baseline = None
    for threads in _THREAD_COUNTS:
        out = kernel.derive_batch(
            array,
            mask_key=_MASK_KEY,
            namespace="h_email",
            truncate=None,
            native_threads=threads,
        )
        if baseline is None:
            baseline = out
            continue
        assert out.type == baseline.type, f"threads={threads}: Arrow type drifted from t1"
        assert out.to_pylist() == baseline.to_pylist(), (
            f"threads={threads}: hash output diverged from t1 (clamp must not change bytes)"
        )


@_NEEDS_COMPANION
def test_index_kernel_is_byte_identical_across_thread_counts() -> None:
    """derive_index_batch (faker/index) output is bit-identical for native_threads in {1, 4, 8}
    across several pool sizes: the clamp shares this resolver with the hash path, so the same
    parity-neutrality must hold here."""
    import decoy_engine_native._kernel as kernel
    import pyarrow as pa

    array = _string_array()
    for pool_size in (1, 2, 97, 100003):
        baseline = None
        for threads in _THREAD_COUNTS:
            out = kernel.derive_index_batch(
                array,
                mask_key=_MASK_KEY,
                namespace="pool.city",
                pool_size=pool_size,
                native_threads=threads,
            )
            assert out.type == pa.uint64()
            if baseline is None:
                baseline = out.to_pylist()
                continue
            assert out.to_pylist() == baseline, (
                f"pool_size={pool_size} threads={threads}: index output diverged from t1"
            )

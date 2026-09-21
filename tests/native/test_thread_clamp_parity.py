"""Thread-invariance regression for the native masking kernels (value AND Arrow type).

The engine-side thread-knee clamp lives in one place, the Rust thread-budget resolver
(`decoy-engine-native/src/threads.rs::NativeThreadBudget::resolve`): a per-call `native_threads`
request is lowered to `min(requested, host_available, knee)`. Its CLAMP ARITHMETIC is guarded on
the Rust side (`threads::tests::resolve_clamps_*`, `resolve_wires_the_env_knee_end_to_end`), which
is where "a request of 8 becomes 4" is actually asserted; there is no Python-observable accessor
for the resolved budget (the compiled kernel exposes only `abi_version`, `derive_batch`,
`derive_index_batch`), so it cannot be asserted from here.

What Python owns is the property the clamp relies on: the masking kernels are thread-INVARIANT, so
lowering a thread grant can never change the bytes. A native job derives each row independently and
arbitrates errors by lowest row index, so worker-thread count changes speed, never output. These
tests assert that invariance (value and Arrow field type) across native_threads in {1, 4, 8} for
both shared callers of the resolver, `derive_batch` (hash) and `derive_index_batch` (faker/index).

The invariance is row-local and would hold even without the clamp; that is the point. It is the
standing guarantee that makes the clamp safe, and the regression that would catch a future kernel
change (a reduction that folded results in thread-completion order, say) that broke it. The tests
skip only when effective parallelism (`min(usable cores, effective knee)`) is below 2, since then
every count in {1, 4, 8} clamps to the same value and the comparison proves nothing; a 2-3 core
host still runs (t1=1 genuinely differs from t4/t8=2-3), and a `DECOY_NATIVE_MASK_THREAD_KNEE=1`
override correctly skips.
"""

from __future__ import annotations

import importlib.util
import os
import re

import pytest

# Rust `usize::parse` grammar (after the resolver trims surrounding whitespace): ASCII digits with an
# optional leading '+', no '-', no Unicode digits, no underscores. Python `int()` would wrongly accept
# Unicode digits (e.g. U+0661 ARABIC-INDIC ONE), so gate on this ASCII pattern first. `[0-9]` in `re`
# is ASCII-only (unlike `\d`).
_KNEE_GRAMMAR = re.compile(r"\+?[0-9]+")

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)


def _usable_cpus() -> int:
    """Best-effort count of cores this process may actually run on, mirroring what the Rust
    resolver clamps against (`std::thread::available_parallelism`). Prefer the scheduler affinity
    mask (respects cgroup/taskset limits) and fall back to `os.cpu_count()`."""
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        return len(getaffinity(0))
    return os.cpu_count() or 1


def _effective_knee() -> int:
    """The knee the Rust resolver will apply, mirroring `DECOY_NATIVE_MASK_THREAD_KNEE` semantics:
    absent or invalid/out-of-range -> the default 4; otherwise the set value in 1..=1024. An
    invalid override would make the kernel calls raise (fail-closed), not silently clamp, so for
    the skip decision we treat it as the default."""
    raw = os.environ.get("DECOY_NATIVE_MASK_THREAD_KNEE")
    if raw is None:
        return 4
    trimmed = raw.strip()  # Rust trims surrounding whitespace before parsing
    if not _KNEE_GRAMMAR.fullmatch(trimmed):
        # A value Rust rejects (Unicode digits, sign, underscores, non-numeric) makes the resolver
        # fail closed rather than clamp; treat it as the default for the skip decision so the test
        # is never hidden by a would-be-invalid override.
        return 4
    value = int(trimmed)
    return value if 1 <= value <= 1024 else 4


# The test compares native_threads in {1, 4, 8}; it is only meaningful if at least one of {4, 8}
# resolves to an effective count > 1, i.e. if min(usable cores, effective knee) >= 2. Below that
# every count clamps to the same value and the comparison proves nothing. This correctly RUNS on
# 2-3 core hosts (where t1=1 genuinely differs from t4/t8=2-3) and skips the knee=1 / 1-core cases.
_EFFECTIVE_PARALLELISM = min(_usable_cpus(), _effective_knee())
_NEEDS_PARALLELISM = pytest.mark.skipif(
    _EFFECTIVE_PARALLELISM < 2,
    reason="effective parallelism < 2: all of {1,4,8} clamp to the same count, so the compare is vacuous",
)

# One representative column: nulls (incl. leading/trailing), duplicates, empty string, ASCII and
# multibyte unicode, and lengths that straddle any internal slice boundary. Kept well above a
# single row so a multi-thread grant actually fans the array into more than one range.
_VALUES = (
    [None, "alice", "bob", "", "alice", "Ada Lovelace", "éèê", "\U0001f600ab"]
    + [None if i % 7 == 0 else f"user-{i}-{'z' * (i % 5)}" for i in range(200)]
    + [None]
)

# On a >= 4-core host: 1 and 4 pass through; a request of 8 is clamped to the default knee (4). All
# three must be byte-identical regardless (that is the invariance under test).
_THREAD_COUNTS = (1, 4, 8)

_MASK_KEY = bytes(range(32))


def _string_array():
    import pyarrow as pa

    return pa.array(_VALUES, type=pa.string())


@_NEEDS_COMPANION
@_NEEDS_PARALLELISM
def test_hash_thread_invariance_value_and_type() -> None:
    """derive_batch (hash) output is bit-identical for native_threads in {1, 4, 8}: lowering an
    8-thread grant to the knee (Rust-side clamp) never perturbs the value or the Arrow type."""
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
            f"threads={threads}: hash output diverged from t1 (thread count must not change bytes)"
        )


@_NEEDS_COMPANION
@_NEEDS_PARALLELISM
def test_index_thread_invariance_value_and_type() -> None:
    """derive_index_batch (faker/index) output is bit-identical for native_threads in {1, 4, 8}
    across several pool sizes: it shares the clamped resolver with the hash path, so the same
    thread-invariance must hold here."""
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

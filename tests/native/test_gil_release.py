"""GIL-release proof for the compiled ``derive_batch`` (Phase 1 Task 1.4).

The acceptance matrix requires PROOF that the native compute releases the GIL, not merely that
concurrent calls agree. The proof is wall-clock parallel speedup: two native computes overlap in
real time ONLY if the GIL is released for the compute, and (unlike counter/timestamp sampling) no
``sys.setswitchinterval`` tuning or boundary GIL handoff can manufacture wall-clock overlap out of
a GIL-serialized workload. A pure-Python CPU loop, which provably holds the GIL, is run through the
same harness in-test to calibrate "no overlap" on the live machine.

This proof assumes a GIL build. On a free-threaded interpreter (3.13t) pure-Python threads run in
parallel too, so the control would also show speedup and the discriminator would need rework before
trusting it on a free-threaded port.
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
from collections.abc import Callable

import pyarrow as pa
import pytest

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)


@_NEEDS_COMPANION
def test_panic_in_detached_region_becomes_coded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A panic INSIDE the GIL-released region must be caught, not abort, and the GIL must be
    restored. Driven by the ``DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_DETACH`` test-only hook, since
    no valid input reaches a panic (every ``derive_array`` path returns ``Result``). Proves both
    layers: ``Python::detach`` restores the GIL on unwind, and the outer ``catch_unwind`` maps
    the panic to the coded ``internal_panic`` error."""
    import decoy_engine_native._kernel as kernel

    values = pa.array(["a", "b", "c"], type=pa.string())
    mask_key = b"\x33" * 32

    monkeypatch.setenv("DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_DETACH", "1")
    with pytest.raises(ValueError, match="internal_panic"):
        kernel.derive_batch(values, mask_key=mask_key, namespace="h_email", truncate=None)

    # The GIL was restored on unwind: a clean call (hook now unset) succeeds in the same process
    # rather than deadlocking, which is what a leaked GIL would cause.
    monkeypatch.delenv("DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_DETACH")
    out = kernel.derive_batch(values, mask_key=mask_key, namespace="h_email", truncate=None)
    assert len(out) == 3


def _elapsed(fn: Callable[[], object], workers: int, concurrent: bool) -> float:
    """Wall-clock to run ``fn`` ``workers`` times, either back-to-back or on ``workers`` threads."""
    if not concurrent:
        t0 = time.monotonic()
        for _ in range(workers):
            fn()
        return time.monotonic() - t0
    threads = [threading.Thread(target=fn) for _ in range(workers)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return time.monotonic() - t0


@_NEEDS_COMPANION
@pytest.mark.skipif(
    (os.cpu_count() or 1) < 2,
    reason="wall-clock parallelism proof needs >=2 cores; single-core cannot overlap threads",
)
def test_native_compute_releases_gil_for_wallclock_parallelism() -> None:
    """Discriminating GIL-release proof by wall-clock parallel speedup, self-calibrated.

    Timestamp/counter-sampling proofs are defeatable: with the GIL held for the whole native
    call, boundary handoffs plus a tuned ``sys.setswitchinterval`` can still sprinkle a waiting
    sentinel's activity into any fixed "interior" window (Codex's committed counterexample:
    ``PyDLL.usleep(1s)`` + ``setswitchinterval(0.25)`` produced dozens of interior samples). So
    those approaches are abandoned.

    This proof instead measures whether two native computes OVERLAP in wall-clock, which no
    switch-interval tuning can manufacture. Run ``W`` copies of the compute back-to-back
    (serial), then on ``W`` threads (concurrent), and take ``speedup = serial / concurrent``:

    * GIL held for the compute  -> the W threads serialize -> concurrent ~= serial -> speedup ~1.
    * GIL released for the compute -> the W computes run on W cores -> concurrent << serial ->
      speedup grows toward min(W, cores).

    A pure-Python CPU loop (which provably holds the GIL) is run through the SAME harness as an
    in-test control: it pins what "GIL-held, no overlap" measures on THIS machine right now, so
    the threshold is a margin over a live baseline rather than a hard-coded constant that a slow
    or loaded box could trip. The kernel must clear both an absolute floor and a clear margin over
    that control.
    """
    from decoy_engine.execution.native._crypto_ext import load_compiled_crypto_kernel

    kernel = load_compiled_crypto_kernel()
    workers = min(4, os.cpu_count() or 1)

    # Control: pure-Python accumulate holds the GIL, so it cannot overlap. Sized to a few hundred
    # ms so thread-startup overhead is negligible against per-call work; the ratio is what counts.
    def busy() -> None:
        x = 0
        for i in range(12_000_000):
            x += i

    # Warm up (import/JIT-free, but primes allocator + branch predictors) then measure the control
    # both ways. A single serial pass of each so the two measurements see the same machine state.
    busy()
    control_serial = _elapsed(busy, workers, concurrent=False)
    control_concurrent = _elapsed(busy, workers, concurrent=True)
    control_speedup = control_serial / control_concurrent

    n = 2_000_000
    values = pa.array(["user@example.com"] * n, type=pa.string())
    mask_key = b"\x11" * 32

    def derive() -> None:
        kernel.derive_batch(values, mask_key=mask_key, namespace="h_email", truncate=None)

    derive()  # warm up
    kernel_serial = _elapsed(derive, workers, concurrent=False)
    kernel_concurrent = _elapsed(derive, workers, concurrent=True)
    kernel_speedup = kernel_serial / kernel_concurrent

    # The control confirms the harness measures ~no overlap for GIL-held work on this box. Allow it
    # some slack (>1.3 would itself be suspicious), but the real discriminator is the kernel
    # clearing both an absolute floor and a clear margin over the live control.
    assert control_speedup < 1.3, (
        f"pure-Python control showed speedup {control_speedup:.2f} (serial {control_serial:.3f}s, "
        f"concurrent {control_concurrent:.3f}s); the harness is not measuring GIL-held work as "
        "serial, so the kernel comparison would be meaningless"
    )
    assert kernel_speedup > 1.5 and kernel_speedup > control_speedup * 1.8, (
        f"native derive_batch showed wall-clock speedup {kernel_speedup:.2f} across {workers} "
        f"threads (serial {kernel_serial:.3f}s, concurrent {kernel_concurrent:.3f}s) vs a "
        f"GIL-held control of {control_speedup:.2f}; the compute does not appear to run in "
        "parallel, so the GIL was not released for the row loop"
    )


@_NEEDS_COMPANION
def test_concurrent_derive_batch_calls_agree() -> None:
    """Two threads deriving concurrently (GIL released for each compute) must both produce the
    correct, identical result. `derive_array` uses only per-call owned data (the imported array
    + a per-batch DeriveContext) and the shared pool is a `Sync` OnceLock, so overlapping calls
    have no shared mutable state to race."""
    from decoy_engine.execution.native._crypto_ext import load_compiled_crypto_kernel

    kernel = load_compiled_crypto_kernel()
    values = pa.array([f"user{i}@example.com" for i in range(50_000)], type=pa.string())
    mask_key = b"\x22" * 32

    expected = kernel.derive_batch(values, mask_key=mask_key, namespace="h_email", truncate=None)

    results: list[pa.Array] = []
    lock = threading.Lock()

    def run() -> None:
        out = kernel.derive_batch(values, mask_key=mask_key, namespace="h_email", truncate=None)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 4
    for out in results:
        assert out.equals(expected), "a concurrent derive_batch call diverged from the reference"

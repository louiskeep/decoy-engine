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
import sys
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


def _effective_cpus() -> int:
    """CPUs this PROCESS may actually run on, not the machine's total.

    ``os.cpu_count()`` reports the box's logical CPUs, but a ``taskset``/cgroup-pinned process
    (common on CI) can only run on a subset; deciding worker count or the single-core skip from
    the machine total would launch threads that cannot overlap and mismeasure the speedup. On
    Linux ``sched_getaffinity`` gives the real per-process allowance; fall back to the total where
    it is unavailable.
    """
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        return len(getaffinity(0))
    return os.cpu_count() or 1


def _gil_enabled() -> bool:
    """True on a normal GIL build; False on a free-threaded (3.13t) interpreter.

    The whole proof rests on "pure-Python threads cannot overlap" as the GIL-held control. On a
    free-threaded build that control would itself show speedup, so the discriminator is invalid
    and the test must skip rather than mismeasure.
    """
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    return bool(is_gil_enabled()) if is_gil_enabled is not None else True


def _elapsed(fn: Callable[[], object], workers: int, concurrent: bool) -> float:
    """Wall-clock to run ``fn`` ``workers`` times, either back-to-back or on ``workers`` threads.

    A worker exception is captured and re-raised on the calling thread: a concurrent-only failure
    that terminated a worker early would otherwise shorten the measured interval and could inflate
    the apparent speedup into a false pass, so the measurement must fail loudly instead.
    """
    if not concurrent:
        t0 = time.monotonic()
        for _ in range(workers):
            fn()
        return time.monotonic() - t0

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def guarded() -> None:
        try:
            fn()
        except BaseException as exc:  # re-raised on the caller below; never swallowed
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=guarded) for _ in range(workers)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    if errors:
        raise errors[0]
    return elapsed


@_NEEDS_COMPANION
@pytest.mark.skipif(
    _effective_cpus() < 2,
    reason="wall-clock parallelism proof needs >=2 usable CPUs; one cannot overlap threads",
)
@pytest.mark.skipif(
    not _gil_enabled(),
    reason="free-threaded build: the pure-Python GIL-held control is invalid, needs separate proof",
)
def test_native_compute_releases_gil_for_wallclock_parallelism() -> None:
    """Discriminating GIL-release proof by wall-clock parallel speedup, self-calibrated.

    Timestamp/counter-sampling proofs are defeatable: with the GIL held for the whole native
    call, boundary handoffs plus a tuned ``sys.setswitchinterval`` can still sprinkle a waiting
    sentinel's activity into any fixed "interior" window (Codex's committed counterexample:
    ``PyDLL.usleep(1s)`` + ``setswitchinterval(0.25)`` produced dozens of interior samples). So
    those approaches are abandoned.

    This proof instead measures whether two native computes OVERLAP in wall-clock, which no
    switch-interval tuning can manufacture for a fixed-work callable. Run ``W`` copies of the
    compute back-to-back (serial), then on ``W`` threads (concurrent), and take
    ``speedup = serial / concurrent``:

    * GIL held for the compute  -> the W threads serialize -> concurrent ~= serial -> speedup ~1.
    * GIL released for the compute -> the W computes run on W cores -> concurrent << serial ->
      speedup grows toward min(W, cores).

    A pure-Python CPU loop (which provably holds the GIL) is run through the SAME harness as an
    in-test control: it pins what "GIL-held, no overlap" measures on THIS machine right now, so
    the threshold is a margin over a live baseline rather than a hard-coded constant that a slow
    or loaded box could trip. To blunt shared-runner scheduling noise BOTH sides take the MEDIAN
    of several trials. Median, not max: contention during a trial's serial numerator inflates that
    trial's ratio, so `max` would preserve exactly the spurious favorable outlier and stop being
    fail-closed (a serialized kernel measuring [1.0, 1.0, 1.9] would pass on the 1.9). Median-of-N
    over the floor means a majority of trials cleared it, and a lone bad trial in either direction
    cannot move it. The kernel must clear both an absolute floor and a clear margin over the live
    control.
    """
    from decoy_engine.execution.native._crypto_ext import load_compiled_crypto_kernel

    kernel = load_compiled_crypto_kernel()
    workers = min(4, _effective_cpus())

    # Control: pure-Python accumulate holds the GIL, so it cannot overlap. Sized to a few hundred
    # ms so thread-startup overhead is negligible against per-call work; the ratio is what counts.
    def busy() -> None:
        x = 0
        for i in range(12_000_000):
            x += i

    n = 2_000_000
    values = pa.array(["user@example.com"] * n, type=pa.string())
    mask_key = b"\x11" * 32

    def derive() -> None:
        kernel.derive_batch(values, mask_key=mask_key, namespace="h_email", truncate=None)

    def speedup(fn: Callable[[], object]) -> float:
        return _elapsed(fn, workers, concurrent=False) / _elapsed(fn, workers, concurrent=True)

    trials = 3
    busy()  # warm up allocator/branch predictors before timing
    derive()
    control_speedups = sorted(speedup(busy) for _ in range(trials))
    kernel_speedups = sorted(speedup(derive) for _ in range(trials))

    # Median of both: fail-closed against a lone inflated trial, robust against a lone depressed
    # one. median-of-3 over the floor <=> at least 2 of 3 trials cleared the floor.
    control_speedup = control_speedups[len(control_speedups) // 2]
    kernel_speedup = kernel_speedups[len(kernel_speedups) // 2]

    # The control confirms the harness measures ~no overlap for GIL-held work on this box. Allow it
    # some slack (>1.3 would itself be suspicious), but the real discriminator is the kernel
    # clearing both an absolute floor and a clear margin over the live control.
    assert control_speedup < 1.3, (
        f"pure-Python control median speedup {control_speedup:.2f} across {trials} trials "
        f"({[f'{s:.2f}' for s in control_speedups]}); the harness is not measuring GIL-held work "
        "as serial, so the kernel comparison would be meaningless"
    )
    assert kernel_speedup > 1.5 and kernel_speedup > control_speedup * 1.8, (
        f"native derive_batch median wall-clock speedup {kernel_speedup:.2f} across {workers} "
        f"threads / {trials} trials ({[f'{s:.2f}' for s in kernel_speedups]}) vs a GIL-held "
        f"control of {control_speedup:.2f}; the compute does not appear to run in parallel, so "
        "the GIL was not released for the row loop"
    )


@_NEEDS_COMPANION
def test_panic_in_a_rayon_worker_becomes_coded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A panic raised INSIDE a Rayon worker (Task 1.5's parallel fill) must propagate out of the
    pool, unwind through the GIL-released region, and surface as the coded ``internal_panic`` error
    rather than aborting. Driven by the ``DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_WORKER`` hook, which
    fires only when ``rayon::current_thread_index()`` is set: observing the coded error therefore
    also proves the range ran on a pool worker, not inline. ``native_threads=2`` over a 2-row batch
    forces two non-empty ranges."""
    import decoy_engine_native._kernel as kernel

    values = pa.array(["a", "b"], type=pa.string())
    mask_key = b"\x44" * 32

    monkeypatch.setenv("DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_WORKER", "1")
    with pytest.raises(ValueError, match="internal_panic"):
        kernel.derive_batch(
            values, mask_key=mask_key, namespace="h_email", truncate=None, native_threads=2
        )

    # The GIL was restored on unwind and the pool is reusable: a clean call succeeds in the same
    # process rather than deadlocking or aborting.
    monkeypatch.delenv("DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_WORKER")
    out = kernel.derive_batch(
        values, mask_key=mask_key, namespace="h_email", truncate=None, native_threads=2
    )
    assert len(out) == 2


@_NEEDS_COMPANION
def test_concurrent_derive_batch_calls_agree() -> None:
    """Four threads deriving concurrently (GIL released for each compute) must all produce the
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

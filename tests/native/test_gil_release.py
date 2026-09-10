"""GIL-release proof for the compiled ``derive_batch`` (Phase 1 Task 1.4).

The acceptance matrix requires PROOF that the native compute releases the GIL, not merely
that concurrent calls agree: a pure-Python sentinel thread must observably make progress
DURING a ``derive_batch`` call. A native extension that holds the GIL for the whole call
blocks every Python thread for its full duration (the interpreter's GIL-switch interval does
not preempt a native frame that never yields), so a held-GIL kernel would leave the sentinel
at ~0 increments; a kernel that detaches for the compute lets the sentinel run on its own OS
thread and advance by many thousands.

The sentinel proof assumes a GIL build: on a free-threaded interpreter (3.13t) the sentinel's
``counter += 1`` would itself be a data race and "blocked sentinel implies held GIL" no longer
holds, so this test would need rework before trusting it on a free-threaded port.
"""

from __future__ import annotations

import importlib.util
import threading
import time

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


@_NEEDS_COMPANION
def test_sentinel_thread_progresses_during_native_compute() -> None:
    from decoy_engine.execution.native._crypto_ext import load_compiled_crypto_kernel

    kernel = load_compiled_crypto_kernel()
    # Large enough that the released-GIL compute window is clearly measurable (~1s on the
    # reference-class host); the payload value is constant so building it is cheap.
    n = 1_000_000
    values = pa.array(["user@example.com"] * n, type=pa.string())
    mask_key = b"\x11" * 32

    counter = 0
    stop = threading.Event()

    def spin() -> None:
        nonlocal counter
        while not stop.is_set():
            counter += 1

    sentinel = threading.Thread(target=spin)
    sentinel.start()
    try:
        time.sleep(0.05)  # let the sentinel warm up while the main thread yields the GIL
        before = counter
        kernel.derive_batch(values, mask_key=mask_key, namespace="h_email", truncate=None)
        advanced = counter - before
    finally:
        stop.set()
        sentinel.join()

    assert advanced > 1000, (
        f"sentinel advanced only {advanced} increments during native compute; the GIL "
        "appears not to have been released for the row loop"
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

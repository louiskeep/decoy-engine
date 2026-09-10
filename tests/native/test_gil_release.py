"""GIL-release proof for the compiled ``derive_batch`` (Phase 1 Task 1.4).

The acceptance matrix requires PROOF that the native compute releases the GIL, not merely
that concurrent calls agree: a pure-Python sentinel thread must observably make progress
DURING a ``derive_batch`` call. A native extension that holds the GIL for the whole call
blocks every Python thread for its full duration (the interpreter's GIL-switch interval does
not preempt a native frame that never yields), so a held-GIL kernel would leave the sentinel
at ~0 increments; a kernel that detaches for the compute lets the sentinel run on its own OS
thread and advance by many thousands.
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

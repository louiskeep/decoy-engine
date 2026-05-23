"""Peak-RSS monitor used by the graph runner.

Second sub-milestone of the runner decomposition (V2.0-A.2 in the V2
sprint plan). Lifts the previously-nested ``_PeakRSSMonitor`` class
out of runner.py into its own module so the runner shrinks toward its
sub-300-LOC facade target and the monitor lifecycle is independently
testable.

Why a context manager: the monitor spawns a background thread that
polls ``psutil.Process().memory_info().rss`` every 200 ms. Cleanup
must run even when an exception (including ``FlagPauseSignal``)
propagates out of the run, otherwise the thread leaks and subsequent
runs accumulate workers. Earlier code combined ``__enter__`` /
``__exit__`` with explicit cleanup calls and missed cases; the
``with`` block in the runner now handles every path through Python's
context-manager protocol, including exceptions that were previously
re-raised before the explicit teardown ran.

Why psutil is optional: the engine ships without psutil as a strict
runtime dependency (it is in `[dev]` extras). If psutil is missing
the monitor degrades to a no-op (peak_rss stays 0; no background
thread). The runner's `_check_memory_pressure` no-ops on
peak_rss == 0 so the missing-psutil path is silent.

Why 200 ms is the sample interval: fast enough to catch peaks during
op execution (where the dual-representation pandas+Arrow cost lands)
without polling so often that the monitor itself becomes the source
of noise in benchmark measurements.

Pattern: explicit cleanup via context manager + daemon polling thread.
Standard pattern; cited in the methodology-registry only if the
project decides peak-RSS monitoring is a load-bearing methodology
worth pinning. For now it is mechanical utility code.
"""
from __future__ import annotations

import threading
from typing import Any


class PeakRSSMonitor:
    """Background thread that polls this process's RSS and tracks peak.

    Use as a context manager:

        with PeakRSSMonitor() as monitor:
            ...                 # work that may allocate
        peak = monitor.peak_rss  # bytes; 0 if psutil was unavailable

    The thread starts on ``__enter__`` and stops on ``__exit__``.
    ``__exit__`` joins the thread with a 1 s timeout so a stuck poll
    cannot deadlock the run; the daemon flag is the final backstop.
    """

    def __init__(self) -> None:
        self.peak_rss: int = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._psutil: Any = None
        try:
            import psutil
            self._psutil = psutil
        except ImportError:
            self._psutil = None

    def __enter__(self) -> PeakRSSMonitor:
        if self._psutil is None:
            return self
        self.peak_rss = self._psutil.Process().memory_info().rss
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _poll(self) -> None:
        process = self._psutil.Process()
        while not self._stop.wait(0.2):
            try:
                rss = process.memory_info().rss
            except self._psutil.NoSuchProcess:
                return
            if rss > self.peak_rss:
                self.peak_rss = rss

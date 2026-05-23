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

import os
import threading
from typing import Any

# Threshold (fraction of system RAM) above which the runner emits a
# memory-pressure warning at the end of a run. Set via env var so
# operators can tune without touching code. Default 0.7 matches the
# documented "70% of system RAM" line in the engineering docs.
MEMORY_WARN_THRESHOLD = float(
    os.environ.get("DECOY_MEMORY_WARN_THRESHOLD", "0.7")
)


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


def check_memory_pressure(
    peak_rss_bytes: int,
    graph_engine_mode: str,
    log: Any,
) -> None:
    """Emit a warning when peak RSS exceeded MEMORY_WARN_THRESHOLD of system RAM.

    Called once after the execution loop with the RSS peak recorded by
    PeakRSSMonitor. No-ops silently when psutil is unavailable or
    peak_rss_bytes is 0. In hybrid mode the warning suggests switching
    to pandas to cut peak memory by ~2x at a throughput cost; in other
    modes it advises moving to a larger host.

    Moved from runner.py (V2.0-A.4, 2026-05-23): the function reads
    nothing from the runner; it only consumes the peak_rss number that
    PeakRSSMonitor already produces. Co-locating both halves of the
    memory-pressure feedback loop makes the module's contract clear.
    """
    if log is None or peak_rss_bytes == 0:
        return
    try:
        import psutil
        total_bytes = psutil.virtual_memory().total
    except Exception:
        return
    fraction = peak_rss_bytes / total_bytes if total_bytes else 0
    if fraction < MEMORY_WARN_THRESHOLD:
        return
    peak_gb = peak_rss_bytes / 1024 / 1024 / 1024
    total_gb = total_bytes / 1024 / 1024 / 1024
    if graph_engine_mode == "hybrid":
        log.warning(
            "Pipeline peak memory: %.1f GB (%d%% of %.1f GB system RAM). "
            "For larger jobs on memory-constrained hosts, set "
            "`engine: pandas` in your pipeline YAML to reduce peak memory "
            "by ~2x (trade-off: ~2-3x slower CPU). See "
            "SHARED_ENGINE_ARCHITECTURE.md.",
            peak_gb, int(fraction * 100), total_gb,
        )
    else:
        log.warning(
            "Pipeline peak memory: %.1f GB (%d%% of %.1f GB system RAM). "
            "Job is memory-tight; consider running on a larger instance.",
            peak_gb, int(fraction * 100), total_gb,
        )

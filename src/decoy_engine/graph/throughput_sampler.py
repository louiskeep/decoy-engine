"""Periodic throughput sampler for the graph runner.

The platform's Reporting > Run page renders a throughput chart from
``JobThroughputSample`` rows. Without a periodic sampler, the engine
emits samples ONLY at node-completion boundaries
(``graph/events.py: emit_node_ok``), so a job whose runtime is dominated
by a single long-running node (e.g. a large mask op) produces zero
chart movement between node boundaries. Operators see an empty chart
during the part of the run they most want to monitor.

This module adds a background heartbeat that emits a cumulative-rate
throughput sample every ``interval_sec`` while the runner is executing.

Design:

  - Computes ``sum(completed_node_rows) / wall_elapsed_seconds``.
  - Emits via the engine's ``Logger.throughput_sample`` adapter (which
    on the platform writes a ``JobThroughputSample`` row).
  - Is intentionally a moving cumulative average, NOT an "instant"
    per-tick rate. Pandas / pyarrow operations are atomic from the
    runner's perspective -- it can't observe per-row progress inside
    a long ``df.transform`` call. Cumulative-throughput-so-far is the
    most honest signal the runner can produce from outside the op.
    The chart's TIMESTAMPS advance every tick even when the in-flight
    node contributes zero new rows, which is what gives the chart
    visible movement during a long mask.
  - Skips emission while no nodes have finished yet (``cumulative == 0``)
    so the chart doesn't open with a misleading "0 r/s" point.
  - Stops cleanly via a ``threading.Event`` that the context-manager
    exit sets; the daemon thread joins with a small grace window
    before the executor returns.
  - Swallows logger exceptions so a transient DB hiccup on the
    platform side can't take down the engine. Next tick may succeed.

Method citation: standard background-heartbeat pattern. Same idea as
``prometheus_client``'s ``start_wsgi_server`` background daemon thread
for /metrics scraping, and Python's ``threading.Timer`` for repeated
callbacks. The cumulative-rate metric matches Prometheus's
``rate(counter[interval])`` semantics: an average over the elapsed
window, not a sample of an instantaneous gauge.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

# Default heartbeat interval. 2s matches the platform's
# ``useLiveJobFacts`` poll cadence in the Reporting page (see
# ``web/src/hooks/useLiveJobFacts.ts: pollMs = 2500``), so each new
# sample lands roughly in time for the next chart refresh.
DEFAULT_INTERVAL_SEC = 2.0


class PeriodicThroughputSampler:
    """Background daemon thread that emits cumulative throughput samples.

    Use as a context manager around the runner's node loop:

        with PeriodicThroughputSampler(
            log=ctx.logger,
            get_cumulative_rows=lambda: sum(...),
            start_time=state.overall_start,
        ):
            for node in plan.order:
                ...

    The thread is a daemon so it never blocks process exit, and the
    context-manager exit joins it with a small grace window so the
    runner's return doesn't race the final emission.
    """

    def __init__(
        self,
        log: Any,
        get_cumulative_rows: Callable[[], int],
        start_time: float,
        *,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
    ) -> None:
        self._log = log
        self._get_cumulative_rows = get_cumulative_rows
        self._start_time = start_time
        self._interval_sec = float(interval_sec)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Track the last cumulative row count we saw so a node-finish
        # event that already emitted its own sample (via
        # ``emit_node_ok``) doesn't get a duplicate cumulative sample
        # at the same value on the next tick. The duplicate is
        # harmless mathematically but pollutes the chart with
        # back-to-back identical points.
        self._last_emitted_rows: int | None = None

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="decoy-throughput-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # Small grace window so a tick in progress can finish its
            # emit before we return. The thread is a daemon so we
            # don't strictly need to join, but joining keeps logs in
            # order for the test suite.
            thread.join(timeout=self._interval_sec + 0.5)
            self._thread = None

    def __enter__(self) -> "PeriodicThroughputSampler":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    # ── internals ───────────────────────────────────────────────────

    def _loop(self) -> None:
        # Lazy import keeps this module light when the runner doesn't
        # actually start a sampler (e.g. preview runs that skip the
        # full executor path).
        from decoy_engine.context import emit_throughput_sample

        while not self._stop.wait(self._interval_sec):
            try:
                rows = int(self._get_cumulative_rows())
            except Exception:
                # Snapshot accessor failed; skip this tick.
                continue
            if rows <= 0:
                continue
            if self._last_emitted_rows == rows:
                # Cumulative hasn't moved since last tick. Don't emit
                # a duplicate point.
                continue
            elapsed = time.monotonic() - self._start_time
            if elapsed <= 0:
                continue
            rate = rows / elapsed
            try:
                emit_throughput_sample(self._log, rate)
                self._last_emitted_rows = rows
            except Exception:
                # Logger / platform DB failure must never crash the
                # engine. Swallow and continue; next tick may succeed.
                continue

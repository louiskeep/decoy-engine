"""Per-strategy timing instrumentation.

V2 PERF.BASE.1 (2026-05-27). Engine-side performance measurement that
captures elapsed wall-clock time and RSS delta around each mask-strategy
invocation. Output rolls up into the run's evidence manifest via the
mask op's `ctx.export` plumbing so the platform-side manifest reader
needs no special integration.

Source pattern:

- `time.perf_counter` for monotonic high-resolution timing
  (Python stdlib docs: https://docs.python.org/3/library/time.html#time.perf_counter).
- `psutil.Process.memory_info().rss` for resident-set-size sampling
  (psutil docs: https://psutil.readthedocs.io/). Already a declared
  engine dependency (see `graph/memory_monitor.py`).
- Thread-local active-collector pattern: collector is bound to the
  current execution thread for the duration of a node's run; absent
  collector means absent overhead. Matches the established
  `contextvars`-style scoping the engine uses elsewhere
  (`decoy_engine.context.ExecutionContext`).

Design constraints:

- Zero overhead when no collector is active. The `timed_strategy`
  context manager returns immediately on entry if `get_active_collector()`
  is None. Verified by the production-safe-gate test in
  `tests/test_instrumentation_timing.py` (<2% overhead on a tight
  no-op loop).
- No behavior change to existing code paths. Strategies do not know
  whether they are being timed; the collector is opt-in from above.
- Thread-safety via `threading.local`. Pipelines are currently single-
  threaded per executor invocation; the design tolerates future
  parallel execution without API change (each worker thread gets its
  own active collector slot).

Limitations:

- `threading.local` does not propagate across `asyncio` tasks. If/when
  the engine gains async execution, this module will need to switch to
  `contextvars.ContextVar` to keep the collector bound to the active
  task rather than the OS thread. That migration is tied to the
  distributed/async work scheduled for end-of-V2 performance
  enhancements; until then, threading.local is correct for the actual
  execution model.
- Memory delta is sampled before/after rather than continuously, so it
  measures net change at the bracket, not the true high-water mark
  within the bracket. Adequate for hot-spot ranking; insufficient for
  fine-grained leak detection (use `tracemalloc` for that).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

import psutil


_PROCESS = psutil.Process()


@dataclass(frozen=True)
class StrategyTimingRecord:
    """One per-strategy timing measurement.

    Captures wall-clock elapsed time + RSS delta around a single strategy
    invocation on a single column.
    """

    strategy_type: str
    column: str
    elapsed_ms: float
    peak_memory_delta_kb: int


@dataclass
class TimingCollector:
    """Thread-bound accumulator for per-strategy timing records.

    Lifecycle: the executor (or test) creates a collector, binds it via
    `use_collector(...)`, runs the work that internally calls
    `timed_strategy(...)`, then reads `records` or `summarize()` for the
    rollup.
    """

    records: list[StrategyTimingRecord] = field(default_factory=list)

    def add(self, record: StrategyTimingRecord) -> None:
        self.records.append(record)

    def summarize(self) -> dict[str, dict[str, float | int]]:
        """Roll up records by strategy_type.

        Returns a dict keyed by strategy_type with aggregate stats:

            {
                "fpe": {
                    "count": 3,            # invocations of this strategy
                    "total_ms": 412.7,     # cumulative elapsed
                    "max_ms": 198.3,       # slowest invocation
                    "peak_delta_kb": 4096, # largest RSS delta seen
                },
                ...
            }

        Caller decides whether to keep the raw records list or just the
        summary. Manifest emission uses the summary; debugging may want
        the raw list.
        """
        agg: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "peak_delta_kb": 0}
        )
        for r in self.records:
            entry = agg[r.strategy_type]
            entry["count"] = int(entry["count"]) + 1
            entry["total_ms"] = float(entry["total_ms"]) + r.elapsed_ms
            entry["max_ms"] = max(float(entry["max_ms"]), r.elapsed_ms)
            entry["peak_delta_kb"] = max(
                int(entry["peak_delta_kb"]), r.peak_memory_delta_kb
            )
        return dict(agg)


# Thread-local active collector. Pipeline execution is single-threaded
# per invocation today; using threading.local rather than a module-level
# global gives us per-thread isolation for free if/when parallel
# execution lands.
_thread_local = threading.local()


def get_active_collector() -> TimingCollector | None:
    """Return the active collector bound to this thread, or None.

    Returning None is the cheap-path signal that no timing should be
    captured. Callers (e.g. `timed_strategy`) use this to short-circuit
    when instrumentation is off.
    """
    return getattr(_thread_local, "collector", None)


@contextmanager
def use_collector(collector: TimingCollector) -> Iterator[TimingCollector]:
    """Bind a TimingCollector as the active collector for the current thread.

    Restores any previously-bound collector on exit (supports nesting,
    though nested timing is not a current use case).

    Example:

        with use_collector(TimingCollector()) as c:
            run_some_work()
            print(c.summarize())
    """
    previous = getattr(_thread_local, "collector", None)
    _thread_local.collector = collector
    try:
        yield collector
    finally:
        _thread_local.collector = previous


def rss_kb() -> int:
    """Process resident-set-size in KB.

    psutil's `memory_info()` call is roughly 10-30us on modern hardware;
    cheap enough to call twice per strategy invocation (before + after)
    or per node (begin + end). Public so the graph executor can use it
    for per-node memory delta tracking without re-importing psutil.
    """
    return int(_PROCESS.memory_info().rss / 1024)


# Module-private alias used by timed_strategy below. Keeps the public
# `rss_kb` name stable while letting internal call sites keep their
# original name.
_rss_kb = rss_kb


@contextmanager
def timed_strategy(strategy_type: str, column: str) -> Iterator[None]:
    """Record elapsed time + RSS delta around a strategy invocation.

    Zero-overhead no-op when no collector is active for this thread.

    Example:

        with timed_strategy("fpe", "ssn"):
            masked = strategy.apply(column, rule)

    The wrapped block can do anything; this context manager only times.
    Strategies do not need to know they are being measured.
    """
    collector = get_active_collector()
    if collector is None:
        # Cheap path: no collector active, do not pay sampling cost.
        yield
        return

    rss_before = _rss_kb()
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        rss_after = _rss_kb()
        peak_delta_kb = max(0, rss_after - rss_before)
        collector.add(
            StrategyTimingRecord(
                strategy_type=strategy_type,
                column=column,
                elapsed_ms=elapsed_ms,
                peak_memory_delta_kb=peak_delta_kb,
            )
        )

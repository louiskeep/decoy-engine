"""Unit tests for graph.throughput_sampler.PeriodicThroughputSampler.

Pins the contract the runner depends on:
  - Sampler emits cumulative-rate samples at ~interval_sec cadence
    while the runner is executing.
  - Never emits before any node has finished (cumulative == 0).
  - Never emits the same cumulative twice in a row (dedupe protects
    the chart from back-to-back identical points when no progress
    happened in a tick).
  - Swallows logger / accessor exceptions so a flaky platform side
    can't crash the engine.
  - Stops cleanly on context-manager exit; no zombie threads.
"""
from __future__ import annotations

import threading
import time

import pytest

from decoy_engine.graph.throughput_sampler import PeriodicThroughputSampler


# Short interval used by every test so the suite stays fast. 0.1s is
# long enough to make the wait-vs-stop race deterministic on a
# normally-loaded CI box.
_INTERVAL = 0.1


class SpyLogger:
    """Captures throughput_sample calls. Thread-safe append."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.samples: list[float] = []

    def throughput_sample(self, rps: float) -> None:
        with self._lock:
            self.samples.append(float(rps))


class FlakyLogger(SpyLogger):
    """Raises on the first emission, then behaves normally. Used to
    confirm the sampler doesn't crash on a transient platform-side
    failure."""

    def __init__(self) -> None:
        super().__init__()
        self._raised = False

    def throughput_sample(self, rps: float) -> None:
        if not self._raised:
            self._raised = True
            raise RuntimeError("simulated platform DB hiccup")
        super().throughput_sample(rps)


# ── basic emission ────────────────────────────────────────────────────────


class TestEmission:
    def test_emits_when_cumulative_grows(self):
        log = SpyLogger()
        rows = {"n": 0}
        with PeriodicThroughputSampler(
            log=log,
            get_cumulative_rows=lambda: rows["n"],
            start_time=time.monotonic(),
            interval_sec=_INTERVAL,
        ):
            # Tick 1: 0 cumulative -> no sample.
            time.sleep(_INTERVAL * 1.5)
            # Tick 2+: cumulative grows -> one sample per tick.
            rows["n"] = 1000
            time.sleep(_INTERVAL * 1.5)
            rows["n"] = 2000
            time.sleep(_INTERVAL * 1.5)
        # At least two emissions (1000-row and 2000-row).
        assert len(log.samples) >= 2
        # All samples are positive rates.
        assert all(s > 0 for s in log.samples)

    def test_no_emission_while_cumulative_zero(self):
        log = SpyLogger()
        with PeriodicThroughputSampler(
            log=log,
            get_cumulative_rows=lambda: 0,
            start_time=time.monotonic(),
            interval_sec=_INTERVAL,
        ):
            time.sleep(_INTERVAL * 4)
        # Sampler fired several ticks but cumulative was 0 -> no
        # misleading "0 rows/sec" samples polluted the chart.
        assert log.samples == []

    def test_no_duplicate_when_cumulative_unchanged(self):
        log = SpyLogger()
        with PeriodicThroughputSampler(
            log=log,
            get_cumulative_rows=lambda: 500,  # constant
            start_time=time.monotonic(),
            interval_sec=_INTERVAL,
        ):
            time.sleep(_INTERVAL * 4)
        # First non-zero tick emits one sample; subsequent ticks see
        # the same cumulative count and skip emission.
        assert len(log.samples) == 1


# ── rate math ─────────────────────────────────────────────────────────────


class TestRateCalculation:
    def test_rate_is_cumulative_over_wall_elapsed(self):
        """rate = rows / (now - start_time). Verified by setting
        start_time well in the past, then reading the first
        non-zero emission."""
        log = SpyLogger()
        start = time.monotonic() - 10.0  # pretend the job has been
                                          # running for 10s already
        with PeriodicThroughputSampler(
            log=log,
            get_cumulative_rows=lambda: 10000,
            start_time=start,
            interval_sec=_INTERVAL,
        ):
            time.sleep(_INTERVAL * 2)
        assert len(log.samples) >= 1
        # Expected: ~10000 rows / ~10s = ~1000 r/s. Allow generous
        # slack for the test sleep overhead.
        rate = log.samples[0]
        assert 800 <= rate <= 1200, (
            f"expected ~1000 r/s for 10k rows / 10s elapsed, got {rate}"
        )


# ── resilience ────────────────────────────────────────────────────────────


class TestResilience:
    def test_logger_exception_does_not_crash_sampler(self):
        """A throughput_sample call that raises must not propagate
        out of the daemon thread; later ticks keep emitting."""
        log = FlakyLogger()
        rows = {"n": 100}
        with PeriodicThroughputSampler(
            log=log,
            get_cumulative_rows=lambda: rows["n"],
            start_time=time.monotonic(),
            interval_sec=_INTERVAL,
        ):
            # First emission attempt raises (FlakyLogger), gets
            # swallowed. Second tick (after rows grow so dedupe
            # doesn't block) should succeed.
            time.sleep(_INTERVAL * 1.5)
            rows["n"] = 200
            time.sleep(_INTERVAL * 1.5)
            rows["n"] = 300
            time.sleep(_INTERVAL * 1.5)
        # At least one successful emission landed after the raise.
        assert len(log.samples) >= 1

    def test_accessor_exception_does_not_crash_sampler(self):
        """A get_cumulative_rows() that raises is treated as 'skip
        this tick' rather than killing the thread."""
        log = SpyLogger()
        calls = {"n": 0}

        def flaky_accessor() -> int:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("accessor blew up")
            return 100

        with PeriodicThroughputSampler(
            log=log,
            get_cumulative_rows=flaky_accessor,
            start_time=time.monotonic(),
            interval_sec=_INTERVAL,
        ):
            time.sleep(_INTERVAL * 5)
        # Eventually the accessor succeeded and the sampler emitted.
        assert len(log.samples) >= 1


# ── lifecycle ─────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_thread_stops_on_context_exit(self):
        """The daemon thread joins cleanly when the context exits."""
        log = SpyLogger()
        rows = {"n": 100}
        sampler = PeriodicThroughputSampler(
            log=log,
            get_cumulative_rows=lambda: rows["n"],
            start_time=time.monotonic(),
            interval_sec=_INTERVAL,
        )
        with sampler:
            time.sleep(_INTERVAL * 2)
        # After the context exits, the thread should be gone.
        # _thread is set to None in stop().
        assert sampler._thread is None

    def test_double_start_is_idempotent(self):
        log = SpyLogger()
        sampler = PeriodicThroughputSampler(
            log=log,
            get_cumulative_rows=lambda: 0,
            start_time=time.monotonic(),
            interval_sec=_INTERVAL,
        )
        sampler.start()
        first_thread = sampler._thread
        sampler.start()  # should NOT spawn a second thread
        assert sampler._thread is first_thread
        sampler.stop()

    def test_stop_without_start_is_safe(self):
        sampler = PeriodicThroughputSampler(
            log=SpyLogger(),
            get_cumulative_rows=lambda: 0,
            start_time=time.monotonic(),
        )
        # Never started -> stop is a no-op, no exception.
        sampler.stop()

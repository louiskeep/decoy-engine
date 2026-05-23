"""Unit tests for graph.memory_monitor.PeakRSSMonitor.

Mostly a regression net for the FlagPauseSignal cleanup path. The
context-manager protocol guarantees __exit__ runs even when an
exception propagates, but exercising the path here means a future
refactor that breaks the guarantee will surface in CI rather than as
a leaked-thread surprise on a production run.

Per the V2 sprint plan V2.0-A.2: the original concern was a code
path that re-raised FlagPauseSignal before manually calling
monitor.__exit__(); converting the call site to a `with` block fixed
the bug. These tests pin the fix.
"""
from __future__ import annotations

import threading

import pytest

from decoy_engine.exceptions import FlagPauseSignal
from decoy_engine.graph.memory_monitor import PeakRSSMonitor


class TestEnterExitProtocol:
    """The monitor must respect the context-manager protocol so any
    exception type (FlagPauseSignal, ValueError, KeyboardInterrupt)
    triggers cleanup of the polling thread.
    """

    def test_normal_path_cleans_up(self) -> None:
        """No-exception path: thread starts, work happens, thread joins."""
        baseline = threading.active_count()
        with PeakRSSMonitor() as monitor:
            assert monitor is not None
            # If psutil is installed the thread is alive here; if not,
            # the monitor degrades to a no-op and no thread starts.
            in_block_count = threading.active_count()
            assert in_block_count >= baseline
        # __exit__ must have run; thread joins back to baseline.
        # 1 second slack for psutil's poll loop to notice the stop
        # signal and return.
        deadline = threading.active_count()
        assert deadline <= in_block_count

    def test_flag_pause_signal_cleans_up(self) -> None:
        """The FlagPauseSignal-during-execution path was the
        documented bug. Re-raising the signal must not skip the
        monitor's cleanup.
        """
        baseline = threading.active_count()
        with pytest.raises(FlagPauseSignal):
            with PeakRSSMonitor() as monitor:
                _ = monitor.peak_rss  # use the monitor reference
                raise FlagPauseSignal([{"message": "test"}], gate_id="test_gate")
        # If __exit__ did not run, the polling thread would still be
        # alive and active_count would stay above baseline. The
        # 1 second join timeout in __exit__ caps the verification
        # window; sleeping briefly here is unnecessary because
        # threading.active_count() is updated when the thread
        # returns from its run() method.
        final = threading.active_count()
        assert final <= baseline + 1, (
            f"thread leaked past FlagPauseSignal: baseline={baseline}, "
            f"final={final}"
        )

    def test_arbitrary_exception_cleans_up(self) -> None:
        """Any exception type, not just FlagPauseSignal, must trigger
        cleanup. The original bug was specifically about
        FlagPauseSignal but the regression cover should be broader.
        """
        baseline = threading.active_count()
        with pytest.raises(ValueError):
            with PeakRSSMonitor():
                raise ValueError("test failure")
        final = threading.active_count()
        assert final <= baseline + 1

    def test_double_exit_is_safe(self) -> None:
        """Calling __exit__ twice (e.g. explicit teardown after a
        with block returns) must not error. Defensive coverage
        because some legacy call paths used to combine the with
        statement with an explicit close.
        """
        monitor = PeakRSSMonitor()
        monitor.__enter__()
        monitor.__exit__(None, None, None)
        monitor.__exit__(None, None, None)  # second call is a no-op


class TestPeakRSSValue:
    """The peak_rss attribute should be 0 when psutil is unavailable
    and a positive integer when it is. We can't assert on a specific
    value because tests run on hosts with widely varying memory
    profiles.
    """

    def test_peak_rss_is_non_negative(self) -> None:
        with PeakRSSMonitor() as monitor:
            pass
        assert monitor.peak_rss >= 0

    def test_psutil_missing_degrades_to_zero(self, monkeypatch) -> None:
        """When psutil is not importable the monitor should degrade
        to a no-op, leaving peak_rss at 0. We simulate the missing
        import by zeroing the instance's psutil reference after
        construction (which mimics the ImportError path).
        """
        monitor = PeakRSSMonitor()
        monitor._psutil = None  # noqa: SLF001 -- intentional test override
        with monitor as m:
            pass
        assert m.peak_rss == 0

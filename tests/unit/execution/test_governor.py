"""Sprint B4: the RUNTIME GOVERNOR (`decoy_engine.execution._governor`).

`docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §3.7 + §11's §3.7
erratum. These are the teeth named in the sprint brief:

  1. A (mocked) child whose VmRSS crosses the hard threshold -> the monitor
     issues a real `SIGKILL` call -> the wrapper sees `oom_killed` -> reroutes
     to the next ladder rung.
  2. A child that stays under the threshold -> no kill, the run completes on
     its first rung.
  3. The reroute ladder: killed on `full_frame` -> `out_of_core`;
     `out_of_core` ineligible -> `sequential`; every rung exhausted -> a
     fail-with-diagnostic `GovernorResult`, never a bare process return code.
  4. A PID that exits mid-poll -> the monitor stops cleanly (no exception).
  5. The poller reads the per-PID `VmRSS` path, never cgroup `memory.current`.
  6. `use_runtime_governor=False` -> no monitor attached, no reroute: exactly
     one `run_pipeline_isolated` call on the ladder's first rung.

Plus ONE real-subprocess integration test (no mocking of `run_pipeline_
isolated` or the VmRSS reader): a real child's real RSS growth trips a real
monitor thread's real `SIGKILL`, and the real reroute ladder runs to a
diagnosed exhaustion -- proving the whole mechanism end to end, not just its
mocked parts.

Dennis review remediation (this module's own hardening pass):

  - MED-1: a recycled-pid race -- between the real child being reaped and
    the monitor's `stop()` being called, the kernel can hand the same pid
    number to an unrelated same-uid process; the monitor must confirm
    `PPid == os.getpid()` from the SAME `/proc/<pid>/status` read before
    killing, and must never kill (or keep polling) on a mismatch or a
    missing `PPid:` line.
  - MED-2: `isolate=False` in `isolated_kwargs` is rejected outright --
    an in-process run never spawns a child, so no monitor can ever attach.
  - MED-3: `_INELIGIBLE_ROUTE_MARKERS` is pinned against the REAL
    `ConfigError` text `decide_execution_route` emits (see
    `TestIneligibleRouteMarkersMatchRealConfigErrorText` below) so message
    drift breaks CI, not production.
  - LOW-1: the flag-off exhausted path surfaces the underlying
    `IsolatedRunResult` on `GovernorResult.result` instead of dropping it.
  - LOW-2: `hard_threshold_fraction` and `poll_interval_s` are bounds-checked.
  - LOW-3: `_RssMonitor.stop()` never joins a thread whose `start()` raised.
"""

from __future__ import annotations

import io
import os
import signal
import time
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.errors import ConfigError
from decoy_engine.execution import _governor, _governor_monitor
from decoy_engine.execution._governor import (
    GovernorResult,
    GovernorTripRecord,
    run_job_with_governor,
)
from decoy_engine.execution._isolated_common import IsolatedRunResult

_ENGINE_VERSION = "governor-test"
_MB = 1024 * 1024

# --------------------------------------------------------------------------
# Shared fixtures (mirrors test_isolated_run.py's small no-FK mask job --
# duplicated locally rather than imported cross-file, matching this test
# package's existing per-file convention).
# --------------------------------------------------------------------------


def _validated_dump(cfg: dict[str, Any]) -> dict[str, Any]:
    return PipelineConfig.model_validate(cfg).model_dump()


def _mask_config(tmp_path, n_cols: int) -> dict[str, Any]:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers.csv"),
                },
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [{"name": f"col{i}", "strategy": "redact"} for i in range(n_cols)],
                },
            ],
            "targets": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                },
            },
        }
    )


def _mask_sources(tmp_path, n_rows: int, n_cols: int) -> dict[str, pa.Table]:
    data = {f"col{i}": [f"value-{i}-{row}" for row in range(n_rows)] for i in range(n_cols)}
    df = pd.DataFrame(data)
    df.to_csv(tmp_path / "customers.csv", index=False)
    return {"customers": pa.Table.from_pandas(df, preserve_index=False)}


def _oom_killed_result(peak_rss_mb: float | None = None) -> IsolatedRunResult:
    return IsolatedRunResult(
        outcome="oom_killed",
        peak_rss_mb=peak_rss_mb,
        outputs=None,
        quality_metrics={},
        table_kinds={},
        returncode=None,
        signal_number=int(signal.SIGKILL),
        error="child killed (SIGKILL)",
        isolated=True,
        pid=4242,
    )


def _completed_result() -> IsolatedRunResult:
    return IsolatedRunResult(
        outcome="completed",
        peak_rss_mb=5.0,
        outputs={"customers": pa.table({"col0": ["REDACTED"]})},
        quality_metrics={},
        table_kinds={"customers": "mask"},
        returncode=0,
        signal_number=None,
        error=None,
        isolated=True,
        pid=4242,
    )


def _crashed_result(error: str) -> IsolatedRunResult:
    return IsolatedRunResult(
        outcome="crashed",
        peak_rss_mb=None,
        outputs=None,
        quality_metrics={},
        table_kinds={},
        returncode=1,
        signal_number=None,
        error=error,
        isolated=True,
        pid=4242,
    )


_OOC_INELIGIBLE_ERROR = (
    "execution_mode='out_of_core' requested but the job is not "
    "out-of-core-eligible (no_relationships)."
)
_SEQUENTIAL_INELIGIBLE_ERROR = (
    "execution_mode='sequential' requested but the job is not "
    "sequential-eligible (no_relationships)."
)


# --------------------------------------------------------------------------
# Tooth 1 + 2: (mocked) VmRSS crossing the hard threshold really calls
# os.kill; staying under never does.
# --------------------------------------------------------------------------


class TestMonitorDrivesRealKillDecision:
    def test_vmrss_crossing_threshold_kills_and_reroutes_to_out_of_core(
        self, tmp_path, monkeypatch
    ):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)
        fake_pid = 999_001
        kill_calls: list[tuple[int, int]] = []
        # `budget_bytes=100MB` -> hard threshold at 0.93*100 = 93MB (default
        # fraction). The mocked VmRSS sequence stays under it twice, then
        # crosses -- proving the monitor polls repeatedly, not just once.
        rss_sequence = iter([10 * _MB, 20 * _MB, 95 * _MB])

        def fake_read_status(pid: int) -> tuple[int, int | None] | None:
            assert pid == fake_pid  # the monitor must poll THIS child, not itself
            rss = next(rss_sequence, None)
            # PPid == this test process's own pid: a genuine (non-recycled)
            # child, so the MED-1 identity guard must not suppress the kill.
            return None if rss is None else (rss, os.getpid())

        def fake_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))

        monkeypatch.setattr(_governor_monitor, "_read_child_status", fake_read_status)
        monkeypatch.setattr(os, "kill", fake_kill)

        calls: list[str] = []

        def fake_run_pipeline_isolated(config, sources, *, execution_mode, on_spawn=None, **kw):
            calls.append(execution_mode)
            if execution_mode == "full_frame":
                on_spawn(fake_pid)
                # Give the monitor thread a bounded window to poll+kill --
                # busy-wait on the spy rather than a fixed sleep so the test
                # is fast on a quiet box and still reliable on a loaded one.
                deadline = time.time() + 5.0
                while not kill_calls and time.time() < deadline:
                    time.sleep(0.005)
                return _oom_killed_result(peak_rss_mb=95.0)
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run_pipeline_isolated)

        result = run_job_with_governor(
            cfg,
            sources,
            budget_bytes=100 * _MB,
            use_runtime_governor=True,
            poll_interval_s=0.01,
            engine_version=_ENGINE_VERSION,
        )

        assert kill_calls == [(fake_pid, int(signal.SIGKILL))]
        assert calls == ["full_frame", "out_of_core"]
        assert isinstance(result, GovernorResult)
        assert result.outcome == "completed"
        assert result.final_route == "out_of_core"
        assert len(result.trips) == 1
        trip = result.trips[0]
        assert trip.route == "full_frame"
        assert trip.trip_kind == "governor_kill"
        assert trip.reroute_to == "out_of_core"
        assert trip.observed_peak_mb == pytest.approx(95.0)

    def test_vmrss_staying_under_threshold_never_kills_and_completes(self, tmp_path, monkeypatch):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)
        fake_pid = 999_002
        kill_calls: list[tuple[int, int]] = []

        def fake_read_status(pid: int) -> tuple[int, int | None] | None:
            return (1 * _MB, os.getpid())  # always far under any plausible threshold

        monkeypatch.setattr(_governor_monitor, "_read_child_status", fake_read_status)
        monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

        def fake_run_pipeline_isolated(config, sources, *, execution_mode, on_spawn=None, **kw):
            if on_spawn is not None:
                on_spawn(fake_pid)
            time.sleep(0.05)  # let the monitor poll a few times before returning
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run_pipeline_isolated)

        result = run_job_with_governor(
            cfg,
            sources,
            budget_bytes=100 * _MB,
            use_runtime_governor=True,
            poll_interval_s=0.005,
            engine_version=_ENGINE_VERSION,
        )

        assert kill_calls == []
        assert result.outcome == "completed"
        assert result.final_route == "full_frame"
        assert result.trips == ()


# --------------------------------------------------------------------------
# Tooth 3: the reroute ladder.
# --------------------------------------------------------------------------


class TestRerouteLadder:
    def test_killed_on_full_frame_reroutes_to_out_of_core(self, tmp_path, monkeypatch):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

        def fake_run_pipeline_isolated(config, sources, *, execution_mode, on_spawn=None, **kw):
            if on_spawn is not None:
                on_spawn(1)
            if execution_mode == "full_frame":
                return _oom_killed_result(peak_rss_mb=200.0)
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run_pipeline_isolated)
        monkeypatch.setattr(_governor_monitor, "_read_child_status", lambda pid: None)

        result = run_job_with_governor(
            cfg, sources, budget_bytes=100 * _MB, use_runtime_governor=True, poll_interval_s=0.01
        )

        assert result.outcome == "completed"
        assert result.final_route == "out_of_core"
        assert [t.route for t in result.trips] == ["full_frame"]

    def test_out_of_core_ineligible_reroutes_to_sequential(self, tmp_path, monkeypatch):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

        def fake_run_pipeline_isolated(config, sources, *, execution_mode, on_spawn=None, **kw):
            if on_spawn is not None:
                on_spawn(1)
            if execution_mode == "full_frame":
                return _oom_killed_result(peak_rss_mb=200.0)
            if execution_mode == "out_of_core":
                return _crashed_result(_OOC_INELIGIBLE_ERROR)
            assert execution_mode == "sequential"
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run_pipeline_isolated)
        monkeypatch.setattr(_governor_monitor, "_read_child_status", lambda pid: None)

        result = run_job_with_governor(
            cfg, sources, budget_bytes=100 * _MB, use_runtime_governor=True, poll_interval_s=0.01
        )

        assert result.outcome == "completed"
        assert result.final_route == "sequential"
        assert [t.route for t in result.trips] == ["full_frame", "out_of_core"]
        ooc_trip = result.trips[1]
        assert ooc_trip.trip_kind == "route_ineligible"
        assert ooc_trip.reroute_to == "sequential"
        assert ooc_trip.observed_peak_mb is None  # never ran long enough to have a peak

    def test_ladder_exhausted_fails_with_diagnostic_never_a_bare_returncode(
        self, tmp_path, monkeypatch
    ):
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)

        def fake_run_pipeline_isolated(config, sources, *, execution_mode, on_spawn=None, **kw):
            if on_spawn is not None:
                on_spawn(1)
            if execution_mode == "sequential":
                return _crashed_result(_SEQUENTIAL_INELIGIBLE_ERROR)
            return _oom_killed_result(peak_rss_mb=300.0)

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run_pipeline_isolated)
        monkeypatch.setattr(_governor_monitor, "_read_child_status", lambda pid: None)

        result = run_job_with_governor(
            cfg, sources, budget_bytes=100 * _MB, use_runtime_governor=True, poll_interval_s=0.01
        )

        assert result.outcome == "exhausted"
        assert result.final_route is None
        assert result.result is None
        assert [t.route for t in result.trips] == ["full_frame", "out_of_core", "sequential"]
        assert result.diagnostic is not None
        # Never an opaque rc137 -- the diagnostic names every route tried and why.
        assert "137" not in result.diagnostic
        assert "full_frame" in result.diagnostic
        assert "out_of_core" in result.diagnostic
        assert "sequential" in result.diagnostic

    def test_genuine_crash_is_not_silently_rerouted(self, tmp_path, monkeypatch):
        """A non-eligibility, non-memory crash on full_frame must surface
        immediately, not burn the rest of the ladder retrying the same bug
        on another route."""
        cfg = _mask_config(tmp_path, n_cols=1)
        sources = _mask_sources(tmp_path, n_rows=5, n_cols=1)
        calls: list[str] = []

        def fake_run_pipeline_isolated(config, sources, *, execution_mode, on_spawn=None, **kw):
            calls.append(execution_mode)
            return _crashed_result("ValueError: a genuine unrelated bug")

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run_pipeline_isolated)

        result = run_job_with_governor(
            cfg, sources, budget_bytes=100 * _MB, use_runtime_governor=True, poll_interval_s=0.01
        )

        assert calls == ["full_frame"]  # never tried out_of_core/sequential
        assert result.outcome == "exhausted"
        assert len(result.trips) == 1
        assert result.trips[0].trip_kind == "crashed"
        assert "genuine unrelated bug" in (result.diagnostic or "")


# --------------------------------------------------------------------------
# Tooth 4: a PID that exits mid-poll stops the monitor cleanly.
# --------------------------------------------------------------------------


class TestPidExitsMidPoll:
    def test_monitor_stops_cleanly_when_pid_vanishes(self, monkeypatch):
        reads = iter([50 * _MB, None])

        def fake_read_status(pid: int) -> tuple[int, int | None] | None:
            rss = next(reads, None)
            return None if rss is None else (rss, os.getpid())

        monkeypatch.setattr(_governor_monitor, "_read_child_status", fake_read_status)
        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

        monitor = _governor._RssMonitor(
            pid=123_456, hard_threshold_bytes=10**12, poll_interval_s=0.01
        )
        monitor.start()
        # Bounded wait for the poll loop to observe the `None` and return on
        # its own -- no exception should ever propagate out of the thread.
        deadline = time.time() + 2.0
        while monitor._thread.is_alive() and time.time() < deadline:
            time.sleep(0.005)
        monitor.stop()  # idempotent join; must return promptly, not hang

        assert not monitor._thread.is_alive()
        assert monitor.tripped is False
        assert kill_calls == []


# --------------------------------------------------------------------------
# Tooth 5: VmRSS, never cgroup memory.current.
# --------------------------------------------------------------------------


class TestReadsVmRssNotMemoryCurrent:
    def test_reads_proc_pid_status_vmrss_line(self, monkeypatch):
        import builtins

        real_open = builtins.open
        seen_paths: list[str] = []

        def selective_open(path, *args, **kwargs):
            path_str = str(path)
            seen_paths.append(path_str)
            if path_str == "/proc/54321/status":
                return io.StringIO("VmPeak:   99999 kB\nVmRSS:    12345 kB\nVmData:   1 kB\n")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", selective_open)

        result = _governor_monitor._read_child_vmrss_bytes(54321)

        assert result == 12345 * 1024
        assert "/proc/54321/status" in seen_paths
        assert not any("memory.current" in p for p in seen_paths)
        assert not any("cgroup" in p for p in seen_paths)

    def test_gone_pid_returns_none_without_raising(self):
        # A real, essentially-guaranteed-not-to-exist pid; /proc/<pid>/status
        # will not exist, and this must return None, never raise.
        assert _governor_monitor._read_child_vmrss_bytes(2**30) is None

    def test_reads_proc_pid_status_ppid_line_from_the_same_snapshot(self, monkeypatch):
        """MED-1: `_read_child_status` parses VmRSS and PPid from ONE read of
        the SAME status blob -- the identity check the monitor makes before
        killing must never be based on a second, separately-racing read."""
        import builtins

        real_open = builtins.open

        def selective_open(path, *args, **kwargs):
            path_str = str(path)
            if path_str == "/proc/54321/status":
                return io.StringIO(
                    "VmPeak:   99999 kB\nVmRSS:    12345 kB\nPPid:\t4242\nVmData:   1 kB\n"
                )
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", selective_open)

        status = _governor_monitor._read_child_status(54321)

        assert status == (12345 * 1024, 4242)

    def test_missing_ppid_line_yields_none_not_a_crash(self, monkeypatch):
        """A status blob with no `PPid:` line at all (unexpected on Linux,
        but not treated as fatal) yields ppid=None -- the monitor's kill
        guard treats that the same as an explicit mismatch."""
        import builtins

        real_open = builtins.open

        def selective_open(path, *args, **kwargs):
            path_str = str(path)
            if path_str == "/proc/54321/status":
                return io.StringIO("VmRSS:    12345 kB\n")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", selective_open)

        assert _governor_monitor._read_child_status(54321) == (12345 * 1024, None)


# --------------------------------------------------------------------------
# Tooth 6: flag off -> no monitor, unchanged behavior.
# --------------------------------------------------------------------------


class TestFlagOff:
    def test_flag_off_makes_exactly_one_call_with_no_monitor_and_completes(
        self, tmp_path, monkeypatch
    ):
        calls: list[dict[str, Any]] = []

        def fake_run_pipeline_isolated(config, sources, **kw):
            calls.append(kw)
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run_pipeline_isolated)

        result = run_job_with_governor(
            _mask_config(tmp_path, n_cols=1),
            _mask_sources(tmp_path, n_rows=5, n_cols=1),
            budget_bytes=100 * _MB,
            use_runtime_governor=False,
        )

        assert len(calls) == 1
        assert calls[0]["execution_mode"] == "full_frame"
        # No monitor attached: on_spawn is either absent or None.
        assert calls[0].get("on_spawn") is None
        assert result.outcome == "completed"
        assert result.final_route == "full_frame"
        assert result.trips == ()

    def test_flag_off_failure_is_exhausted_with_no_reroute_attempted(self, tmp_path, monkeypatch):
        calls: list[str] = []

        def fake_run_pipeline_isolated(config, sources, *, execution_mode, **kw):
            calls.append(execution_mode)
            return _oom_killed_result(peak_rss_mb=500.0)

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run_pipeline_isolated)

        result = run_job_with_governor(
            _mask_config(tmp_path, n_cols=1),
            _mask_sources(tmp_path, n_rows=5, n_cols=1),
            budget_bytes=100 * _MB,
            use_runtime_governor=False,
        )

        assert calls == ["full_frame"]  # never tried a second route
        assert result.outcome == "exhausted"
        assert result.trips == ()
        assert result.diagnostic is not None
        assert "use_runtime_governor=False" in result.diagnostic
        # LOW-1: the underlying IsolatedRunResult must be surfaced, not
        # dropped -- callers keep the diagnostics (outcome/peak/error) even
        # though the ladder itself never ran.
        assert result.result is not None
        assert result.result.outcome == "oom_killed"
        assert result.result.peak_rss_mb == pytest.approx(500.0)
        assert result.result.error == "child killed (SIGKILL)"


# --------------------------------------------------------------------------
# Call-contract validation.
# --------------------------------------------------------------------------


class TestCallContractValidation:
    def test_on_spawn_in_isolated_kwargs_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="on_spawn"):
            run_job_with_governor(
                _mask_config(tmp_path, n_cols=1),
                _mask_sources(tmp_path, n_rows=5, n_cols=1),
                budget_bytes=100 * _MB,
                on_spawn=lambda pid: None,
            )

    def test_execution_mode_in_isolated_kwargs_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="execution_mode"):
            run_job_with_governor(
                _mask_config(tmp_path, n_cols=1),
                _mask_sources(tmp_path, n_rows=5, n_cols=1),
                budget_bytes=100 * _MB,
                execution_mode="full_frame",
            )

    def test_empty_ladder_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="ladder"):
            run_job_with_governor(
                _mask_config(tmp_path, n_cols=1),
                _mask_sources(tmp_path, n_rows=5, n_cols=1),
                budget_bytes=100 * _MB,
                ladder=(),
            )

    def test_non_positive_budget_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="budget_bytes"):
            run_job_with_governor(
                _mask_config(tmp_path, n_cols=1),
                _mask_sources(tmp_path, n_rows=5, n_cols=1),
                budget_bytes=0,
            )

    def test_isolate_false_in_isolated_kwargs_is_rejected(self, tmp_path):
        """MED-2: `isolate=False` runs `run_pipeline_isolated` IN-PROCESS,
        which never calls `on_spawn` -- no monitor ever attaches, and a real
        OOM kills the driver process itself (the exact rc137 this module
        exists to prevent). Must fail fast, never silently defeat the
        governor."""
        with pytest.raises(ValueError, match="isolate"):
            run_job_with_governor(
                _mask_config(tmp_path, n_cols=1),
                _mask_sources(tmp_path, n_rows=5, n_cols=1),
                budget_bytes=100 * _MB,
                isolate=False,
            )

    def test_isolate_true_in_isolated_kwargs_is_still_accepted(self, tmp_path, monkeypatch):
        """Companion sanity check: an explicit (redundant) `isolate=True` is
        NOT rejected -- only the unsupervisable `False` value is."""

        def fake_run_pipeline_isolated(config, sources, **kw):
            assert kw["isolate"] is True
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run_pipeline_isolated)

        result = run_job_with_governor(
            _mask_config(tmp_path, n_cols=1),
            _mask_sources(tmp_path, n_rows=5, n_cols=1),
            budget_bytes=100 * _MB,
            isolate=True,
        )
        assert result.outcome == "completed"

    @pytest.mark.parametrize("bad_fraction", [0, -0.1, 1.5, 1.0001])
    def test_out_of_range_hard_threshold_fraction_is_rejected(self, tmp_path, bad_fraction):
        with pytest.raises(ValueError, match="hard_threshold_fraction"):
            run_job_with_governor(
                _mask_config(tmp_path, n_cols=1),
                _mask_sources(tmp_path, n_rows=5, n_cols=1),
                budget_bytes=100 * _MB,
                hard_threshold_fraction=bad_fraction,
            )

    def test_hard_threshold_fraction_of_exactly_one_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_governor, "run_pipeline_isolated", lambda *a, **k: _completed_result())
        result = run_job_with_governor(
            _mask_config(tmp_path, n_cols=1),
            _mask_sources(tmp_path, n_rows=5, n_cols=1),
            budget_bytes=100 * _MB,
            hard_threshold_fraction=1.0,
        )
        assert result.outcome == "completed"

    @pytest.mark.parametrize("bad_interval", [0, -1, -0.5])
    def test_non_positive_poll_interval_is_rejected(self, tmp_path, bad_interval):
        with pytest.raises(ValueError, match="poll_interval_s"):
            run_job_with_governor(
                _mask_config(tmp_path, n_cols=1),
                _mask_sources(tmp_path, n_rows=5, n_cols=1),
                budget_bytes=100 * _MB,
                poll_interval_s=bad_interval,
            )


# --------------------------------------------------------------------------
# MED-1: kill-by-recycled-PID race -- the monitor must confirm PPid identity
# before killing, never trusting that a pid number still means the same
# process it was told to watch.
# --------------------------------------------------------------------------


class TestRecycledPidKillGuard:
    def test_ppid_mismatch_never_kills(self, monkeypatch):
        """THE MED-1 fix: a status read whose PPid does NOT match this
        driver's own pid must never trigger os.kill, no matter how far over
        the hard threshold VmRSS reads -- this is exactly the shape a
        recycled pid produces (the real child already exited and got
        reaped; the kernel handed the pid number to an unrelated same-uid
        process before this monitor's stop() was called)."""
        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
        other_ppid = os.getpid() + 1  # guaranteed not to equal our own pid
        monkeypatch.setattr(
            _governor_monitor, "_read_child_status", lambda pid: (10**12, other_ppid)
        )

        monitor = _governor._RssMonitor(pid=999_999, hard_threshold_bytes=1, poll_interval_s=0.01)
        monitor.start()
        deadline = time.time() + 2.0
        while monitor._thread.is_alive() and time.time() < deadline:
            time.sleep(0.005)
        monitor.stop()

        assert not monitor._thread.is_alive()
        assert kill_calls == []
        assert monitor.tripped is False
        # The over-threshold read is still recorded as a peak sample -- the
        # identity guard only gates the KILL, not diagnostic observation.
        assert monitor.peak_observed_bytes == 10**12

    def test_missing_ppid_never_kills(self, monkeypatch):
        """`ppid=None` (no `PPid:` line in the status blob at all) is
        treated the same as an explicit mismatch: identity unconfirmed, so
        no kill."""
        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
        monkeypatch.setattr(_governor_monitor, "_read_child_status", lambda pid: (10**12, None))

        monitor = _governor._RssMonitor(pid=999_998, hard_threshold_bytes=1, poll_interval_s=0.01)
        monitor.start()
        deadline = time.time() + 2.0
        while monitor._thread.is_alive() and time.time() < deadline:
            time.sleep(0.005)
        monitor.stop()

        assert kill_calls == []
        assert monitor.tripped is False

    def test_ppid_match_still_kills(self, monkeypatch):
        """Sanity companion: a genuine PPid match (the common, non-recycled
        case) still kills -- the guard must not be a blanket suppressor."""
        kill_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
        monkeypatch.setattr(
            _governor_monitor, "_read_child_status", lambda pid: (10**12, os.getpid())
        )

        monitor = _governor._RssMonitor(pid=999_997, hard_threshold_bytes=1, poll_interval_s=0.01)
        monitor.start()
        deadline = time.time() + 2.0
        while monitor._thread.is_alive() and time.time() < deadline:
            time.sleep(0.005)
        monitor.stop()

        assert kill_calls == [(999_997, int(signal.SIGKILL))]
        assert monitor.tripped is True


# --------------------------------------------------------------------------
# LOW-3: Thread.start() failing (thread exhaustion) must not cause stop() to
# raise RuntimeError on a never-started thread.
# --------------------------------------------------------------------------


class TestMonitorStopGuardsNeverStartedThread:
    def test_stop_after_start_raises_does_not_raise(self, monkeypatch):
        monitor = _governor._RssMonitor(pid=123, hard_threshold_bytes=10**9, poll_interval_s=0.01)

        def boom() -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(monitor._thread, "start", boom)

        with pytest.raises(RuntimeError):
            monitor.start()

        monitor.stop()  # must NOT raise -- the thread was never started

    def test_stop_after_successful_start_still_joins_normally(self, monkeypatch):
        """Companion sanity check: the guard must not turn `stop()` into a
        universal no-op -- a monitor that DID start still joins its thread
        as before."""
        monkeypatch.setattr(
            _governor_monitor, "_read_child_status", lambda pid: None
        )  # "gone" immediately
        monitor = _governor._RssMonitor(pid=123, hard_threshold_bytes=10**9, poll_interval_s=0.01)
        monitor.start()
        monitor.stop()
        assert not monitor._thread.is_alive()


# --------------------------------------------------------------------------
# Real-subprocess integration test: no mocking of run_pipeline_isolated or
# the VmRSS reader. A real child's real RSS growth trips a real monitor's
# real SIGKILL, then the real reroute ladder runs to a diagnosed exhaustion
# (this fixture has no relationships block, so out_of_core/sequential are
# both genuinely ineligible for it -- proving the ladder's ineligibility
# detection against REAL `ConfigError` messages crossing the process
# boundary, not a mocked stand-in for one).
# --------------------------------------------------------------------------


class TestRealSubprocessIntegration:
    def test_real_governor_kills_real_child_and_reroutes_to_diagnosed_exhaustion(self, tmp_path):
        cfg = _mask_config(tmp_path, n_cols=6)
        # Enough real rows that the full_frame child is still doing real work
        # (CSV read + DataFrame build) when the poller's first few samples
        # land, rather than the job racing to completion before any poll.
        sources = _mask_sources(tmp_path, n_rows=100_000, n_cols=6)

        # Deliberately tiny budget: interpreter + pandas/pyarrow/duckdb
        # imports alone comfortably exceed this, so the hard threshold
        # (0.93 * budget) is crossed by real RSS growth almost immediately --
        # "a small child that deliberately grows RSS past a low threshold."
        budget_bytes = 40 * _MB

        result = run_job_with_governor(
            cfg,
            sources,
            budget_bytes=budget_bytes,
            use_runtime_governor=True,
            poll_interval_s=0.02,
            engine_version=_ENGINE_VERSION,
        )

        assert isinstance(result, GovernorResult)
        assert result.outcome == "exhausted"
        assert result.result is None
        assert [t.route for t in result.trips] == ["full_frame", "out_of_core", "sequential"]

        first_trip = result.trips[0]
        assert isinstance(first_trip, GovernorTripRecord)
        # A REAL kill: the monitor observed real RSS at/above the threshold
        # and issued a real SIGKILL (not a mocked stand-in).
        assert first_trip.trip_kind == "governor_kill"
        assert first_trip.observed_peak_mb is not None
        assert (
            first_trip.observed_peak_mb * _MB
            >= budget_bytes * _governor._DEFAULT_HARD_THRESHOLD_FRACTION
        )

        # The later rungs (out_of_core, sequential) are genuinely ineligible
        # for this no-relationships fixture (`ConfigError` from `decide_
        # execution_route`'s forced-mode branches) -- but at this
        # deliberately tiny 40MB budget the child's own interpreter/pandas/
        # pyarrow/duckdb imports can ALSO cross the hard threshold before it
        # ever reaches that check, so either a real governor kill or a real
        # ineligibility crash is a correct outcome for them; either way,
        # never a bare/uncleaned oom_killed-less crash.
        for later_trip in result.trips[1:]:
            assert later_trip.trip_kind in ("governor_kill", "route_ineligible")

        assert result.diagnostic is not None
        assert "137" not in result.diagnostic


# --------------------------------------------------------------------------
# MED-3 sentry: `_INELIGIBLE_ROUTE_MARKERS` must stay a real substring of the
# ACTUAL `ConfigError` text `decide_execution_route` emits for each
# forced-mode ineligibility. The governor classifies a rerouted rung's
# crash as `route_ineligible` (skip to the next rung) vs `crashed` (give
# up) purely by matching these marker strings against the error text that
# crosses the isolated-process boundary -- correct today, but brittle: if
# `_pipeline_routing.py`'s wording ever drifts, a genuinely-ineligible rung
# would silently misclassify as `crashed`, and the governor would give up
# instead of skipping to the next rung. This test calls
# `decide_execution_route` DIRECTLY (no process boundary, no mocking of
# its error text) for every marker in the table, so drift breaks CI here,
# never production.
# --------------------------------------------------------------------------


class TestIneligibleRouteMarkersMatchRealConfigErrorText:
    class _FakeProfile:
        def __init__(self, relationships: tuple[Any, ...]) -> None:
            self.relationships = relationships

    @staticmethod
    def _acyclic_graph():
        from decoy_engine.relationships._graph import (
            OrphanPolicy,
            RelationshipEdge,
            RelationshipGraph,
        )

        return RelationshipGraph(
            edges=(
                RelationshipEdge(
                    parent_table="parent",
                    parent_columns=("id",),
                    child_table="child",
                    child_columns=("parent_id",),
                    namespace="ns",
                    orphan_policy=OrphanPolicy.PRESERVE,
                ),
            ),
            ordering=(),
        )

    @staticmethod
    def _cyclic_graph():
        from decoy_engine.relationships._graph import (
            OrphanPolicy,
            RelationshipEdge,
            RelationshipGraph,
        )

        edge_ab = RelationshipEdge(
            parent_table="a",
            parent_columns=("id",),
            child_table="b",
            child_columns=("ref_a",),
            namespace="na",
            orphan_policy=OrphanPolicy.PRESERVE,
        )
        edge_ba = RelationshipEdge(
            parent_table="b",
            parent_columns=("id",),
            child_table="a",
            child_columns=("ref_b",),
            namespace="nb",
            orphan_policy=OrphanPolicy.PRESERVE,
        )
        return RelationshipGraph(edges=(edge_ab, edge_ba), ordering=())

    def _decide(self, **overrides: Any):
        from decoy_engine.execution._pipeline_routing import decide_execution_route

        kwargs: dict[str, Any] = {
            "has_generate_table": False,
            "has_mask_table": True,
            "validators": [],
            "fidelity_report": False,
            "vault_writer": None,
            "execution_mode": "auto",
            "graph": self._acyclic_graph(),
            "resolved_substrate": "pandas",
            "out_of_core_compatible": True,
            "out_of_core_reject_code": None,
            "largest_table_rows": 1_000,
            "out_of_core_threshold_rows": 100,
            "full_frame_reject_rows": 500,
        }
        profile = overrides.pop("profile", self._FakeProfile((object(),)))
        kwargs.update(overrides)
        return decide_execution_route(profile, **kwargs)

    def _assert_marker_matches(self, marker: str, **decide_overrides: Any) -> None:
        with pytest.raises(ConfigError) as exc:
            self._decide(**decide_overrides)
        assert marker in str(exc.value), (
            f"_INELIGIBLE_ROUTE_MARKERS marker {marker!r} is no longer a "
            f"substring of decide_execution_route's real message: {str(exc.value)!r}"
        )

    def test_every_declared_marker_is_covered_by_this_test(self) -> None:
        # Belt-and-suspenders: a future edit that adds a new marker to the
        # governor's table without a matching case below must fail loudly
        # HERE instead of silently under-covering the sentry.
        covered = {
            ("out_of_core", "is not out-of-core-eligible"),
            ("out_of_core", "is not out-of-core-compatible"),
            ("out_of_core", "no mask-kind table to run through the out-of-core path"),
            ("sequential", "is not sequential-eligible"),
            ("sequential", "FK graph has a cross-table cycle"),
            ("sequential", "no mask-kind table to run through the sequential path"),
        }
        declared = {
            (route, marker)
            for route, markers in _governor._INELIGIBLE_ROUTE_MARKERS.items()
            for marker in markers
        }
        assert declared == covered

    def test_out_of_core_not_eligible_marker(self) -> None:
        self._assert_marker_matches(
            "is not out-of-core-eligible",
            execution_mode="out_of_core",
            has_generate_table=True,  # disqualifies _sequential_eligible -> not OOC-eligible
        )

    def test_out_of_core_not_compatible_marker(self) -> None:
        self._assert_marker_matches(
            "is not out-of-core-compatible",
            execution_mode="out_of_core",
            out_of_core_compatible=False,
        )

    def test_out_of_core_no_mask_table_marker(self) -> None:
        self._assert_marker_matches(
            "no mask-kind table to run through the out-of-core path",
            execution_mode="out_of_core",
            has_mask_table=False,
        )

    def test_sequential_not_eligible_marker(self) -> None:
        self._assert_marker_matches(
            "is not sequential-eligible",
            execution_mode="sequential",
            has_generate_table=True,
        )

    def test_sequential_cross_table_cycle_marker(self) -> None:
        self._assert_marker_matches(
            "FK graph has a cross-table cycle",
            execution_mode="sequential",
            graph=self._cyclic_graph(),
        )

    def test_sequential_no_mask_table_marker(self) -> None:
        self._assert_marker_matches(
            "no mask-kind table to run through the sequential path",
            execution_mode="sequential",
            has_mask_table=False,
        )

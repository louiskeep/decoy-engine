"""TQ mutation kills for the runtime governor's ORCHESTRATION functions
(`decoy_engine.execution._governor`, branch `tq/isolated-substrate-grade`).

Scope: the five orchestration functions `run_job_with_governor`,
`_run_one_rung`, `_run_flag_off`, `_is_route_ineligible_error`,
`_observed_peak_mb` (`_validate_call` + `_exhausted_diagnostic` are a sibling
file). Every test here asserts an EXACT machine field the mutation flips -- a
reroute-ladder decision/outcome, a rung selection, a route-ineligible verdict,
an observed-peak value, a flag-off short-circuit, or a forwarded arg -- and
monkeypatches `run_pipeline_isolated` / the RSS reader to a controlled
in-process fake (same pattern as `test_governor.py`) so the GOVERNOR's own
decision logic runs in-process, never a real child.

These are fast unit kills; they do not overlap the teeth already covered by
`test_governor.py` (which asserts the happy-path ladder but leaves the trip
FIELDS, the self-OOM classifier, the arg forwarding, and the helper edge
values unpinned -- exactly what mutmut survived on).
"""

from __future__ import annotations

import signal
import types
from typing import Any

import pytest

from decoy_engine.execution import _governor, _governor_monitor
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._governor import (
    GovernorTripRecord,
    _is_route_ineligible_error,
    _observed_peak_mb,
    run_job_with_governor,
)
from decoy_engine.execution._isolated_common import IsolatedRunResult

_MB = 1024 * 1024
_BUDGET = 100 * _MB

# Opaque sentinels: `run_job_with_governor` only ever FORWARDS config/sources
# (it never inspects them), so identity-checkable placeholders let the
# forwarding-arg mutants (config->None / sources->None) be pinned precisely.
_CFG: dict[str, Any] = {"sentinel": "config"}
_SRC: dict[str, Any] = {"sentinel": "sources"}

_OOC_INELIGIBLE_ERROR = (
    "execution_mode='out_of_core' requested but the job is not "
    "out-of-core-eligible (no_relationships)."
)


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
        outputs={},
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


def _no_child(monkeypatch) -> None:
    """Governor-on path: the monitor's status reader always reports the child
    as gone, so the real `_RssMonitor` thread never trips and exits promptly.
    A monitor is still ATTACHED (on_spawn is called), so `monitor is not None`
    but `monitor.tripped is False` -- the self-OOM shape."""
    monkeypatch.setattr(_governor_monitor, "_read_child_status", lambda pid: None)


# ==========================================================================
# `_is_route_ineligible_error` (#2, #4, #6, #8): the route-ineligible verdict.
# ==========================================================================


class TestIsRouteIneligibleError:
    def test_empty_error_is_never_ineligible(self):
        # #2: `if not error: return False` -> `return True`. A missing error
        # string must NOT be read as an ineligibility verdict.
        assert _is_route_ineligible_error("out_of_core", None) is False
        assert _is_route_ineligible_error("out_of_core", "") is False

    def test_error_without_any_marker_is_not_ineligible(self):
        # #4: `marker in error` -> `marker not in error`. An unrelated error
        # (no marker present) must be False; the inverted membership would
        # report True because every marker is absent.
        assert _is_route_ineligible_error("out_of_core", "some unrelated failure") is False
        # Correctness companion: a real marker IS ineligible.
        assert _is_route_ineligible_error("out_of_core", _OOC_INELIGIBLE_ERROR) is True

    def test_unmapped_route_falls_back_to_empty_markers_not_none(self):
        # #6 / #8: the `.get(route, ())` default -> `None` (both the explicit
        # `None` and the dropped-arg form). The `()` fallback must keep an
        # unmapped route a clean `False`; a `None` default makes `any(... for
        # marker in None)` raise TypeError instead.
        assert _is_route_ineligible_error("bogus_route", "some error text") is False


# ==========================================================================
# `_observed_peak_mb` (#1, #3, #4): monitor-vs-result peak selection.
# ==========================================================================


class TestObservedPeakMb:
    @staticmethod
    def _monitor(peak_bytes: int) -> Any:
        return types.SimpleNamespace(peak_observed_bytes=peak_bytes)

    def test_no_monitor_uses_result_peak(self):
        # #1: `monitor is not None and ...` -> `or`. With monitor=None the
        # `and` short-circuits to the result's own peak; `or` would evaluate
        # `None.peak_observed_bytes` and raise AttributeError.
        result = _completed_result()  # peak_rss_mb=5.0
        assert _observed_peak_mb(None, result) == pytest.approx(5.0)

    def test_zero_observed_bytes_falls_through_to_result_peak(self):
        # #3: `peak_observed_bytes > 0` -> `>= 0`. A monitor that observed
        # nothing (0 bytes) must fall through to the result's peak, not report
        # a bogus 0.0 MB peak.
        result = _completed_result()  # peak_rss_mb=5.0
        assert _observed_peak_mb(self._monitor(0), result) == pytest.approx(5.0)

    def test_one_observed_byte_uses_the_monitor_peak(self):
        # #4: `peak_observed_bytes > 0` -> `> 1`. A single observed byte is a
        # real (>0) observation and must be reported from the monitor, not
        # discarded in favor of the result's peak.
        result = _completed_result()  # peak_rss_mb=5.0 (distinct from 1 byte)
        assert _observed_peak_mb(self._monitor(1), result) == pytest.approx(1 / _MB)


# ==========================================================================
# `_run_flag_off` (#2, #3, #14) + the run_job_with_governor->_run_flag_off
# hand-off (#24, #25): flag-off forwards config/sources and keeps the result.
# ==========================================================================


class TestFlagOffForwardingAndResult:
    def test_flag_off_forwards_config_and_sources_and_keeps_result(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_run(config, sources, *, execution_mode, **kw):
            captured["config"] = config
            captured["sources"] = sources
            captured["execution_mode"] = execution_mode
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)

        result = run_job_with_governor(_CFG, _SRC, budget_bytes=_BUDGET, use_runtime_governor=False)

        # #24 / _run_flag_off#2 (config->None) and #25 / _run_flag_off#3
        # (sources->None): the exact objects must be forwarded through.
        assert captured["config"] is _CFG
        assert captured["sources"] is _SRC
        assert captured["execution_mode"] == "full_frame"
        # _run_flag_off#14: completed branch `result=result` -> `result=None`.
        assert result.outcome == "completed"
        assert result.result is not None
        assert result.result.outcome == "completed"


# ==========================================================================
# `run_job_with_governor` gate + require_bool naming (#1, #2, #6, #7).
# ==========================================================================


class TestGateAndKnobValidation:
    def test_default_is_governor_on_reroutes_when_arg_omitted(self, monkeypatch):
        # #1: the `use_runtime_governor: bool = True` default -> `False`. With
        # the arg OMITTED the governor must be ON (monitor + reroute), not
        # silently degraded to the single-call flag-off path.
        _no_child(monkeypatch)
        calls: list[str] = []

        def fake_run(config, sources, *, execution_mode, on_spawn=None, **kw):
            calls.append(execution_mode)
            if on_spawn is not None:
                on_spawn(1)
            if execution_mode == "full_frame":
                return _oom_killed_result(peak_rss_mb=200.0)
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)

        result = run_job_with_governor(_CFG, _SRC, budget_bytes=_BUDGET, poll_interval_s=0.01)

        assert calls == ["full_frame", "out_of_core"]  # rerouted: governor was ON
        assert result.outcome == "completed"
        assert result.final_route == "out_of_core"

    def test_non_bool_knob_error_names_the_governor_flag(self, monkeypatch):
        # #2 (name->None), #6 ("XX..XX"), #7 (upper): the require_bool name
        # arg. A non-bool value must be rejected naming THIS knob exactly.
        with pytest.raises(ExecutionError) as exc:
            run_job_with_governor(_CFG, _SRC, budget_bytes=_BUDGET, use_runtime_governor="yes")
        assert "use_runtime_governor must be a bool" in str(exc.value)


# ==========================================================================
# `run_job_with_governor` + `_run_one_rung` arg forwarding (#39, #40, #11, #12)
# and the completed-run result field (#56).
# ==========================================================================


class TestGovernorOnForwardingAndCompleted:
    def test_governor_on_forwards_config_sources_and_keeps_completed_result(self, monkeypatch):
        _no_child(monkeypatch)
        captured: dict[str, Any] = {}

        def fake_run(config, sources, *, execution_mode, on_spawn=None, **kw):
            captured["config"] = config
            captured["sources"] = sources
            if on_spawn is not None:
                on_spawn(1)
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)

        result = run_job_with_governor(_CFG, _SRC, budget_bytes=_BUDGET, poll_interval_s=0.01)

        # #39 / _run_one_rung#11 (config->None) and #40 / _run_one_rung#12
        # (sources->None): the SAME objects must reach the rung's runner.
        assert captured["config"] is _CFG
        assert captured["sources"] is _SRC
        # #56: completed return `result=result` -> `result=None`.
        assert result.outcome == "completed"
        assert result.final_route == "full_frame"
        assert result.result is not None
        assert result.result.outcome == "completed"


# ==========================================================================
# `run_job_with_governor` trip-record fields per branch.
#   route_ineligible trip: #80 (budget_bytes), #83 (error)
#   crashed (genuine) trip: #96 (route), #97 (budget_bytes)
#   oom trip + classifier: #132 (self_oom vs governor_kill), #141
#     (budget_bytes), #145 (error), #154/#155 (the "self_oom" literal)
#   shared reroute callback: #158 (on_trip receives the record, not None)
# ==========================================================================


class TestRerouteTripFields:
    def test_route_ineligible_trip_carries_budget_and_error(self, monkeypatch):
        _no_child(monkeypatch)

        def fake_run(config, sources, *, execution_mode, on_spawn=None, **kw):
            if on_spawn is not None:
                on_spawn(1)
            if execution_mode == "full_frame":
                return _oom_killed_result(peak_rss_mb=200.0)
            if execution_mode == "out_of_core":
                return _crashed_result(_OOC_INELIGIBLE_ERROR)
            return _completed_result()  # sequential

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)

        result = run_job_with_governor(_CFG, _SRC, budget_bytes=_BUDGET, poll_interval_s=0.01)

        ineligible = result.trips[1]
        assert ineligible.trip_kind == "route_ineligible"
        assert ineligible.budget_bytes == _BUDGET  # #80
        assert ineligible.error == _OOC_INELIGIBLE_ERROR  # #83

    def test_genuine_crash_trip_carries_route_and_budget(self, monkeypatch):
        def fake_run(config, sources, *, execution_mode, on_spawn=None, **kw):
            return _crashed_result("ValueError: a genuine unrelated bug")

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)

        result = run_job_with_governor(_CFG, _SRC, budget_bytes=_BUDGET, poll_interval_s=0.01)

        assert result.outcome == "exhausted"
        crashed = result.trips[0]
        assert crashed.trip_kind == "crashed"
        assert crashed.route == "full_frame"  # #96
        assert crashed.budget_bytes == _BUDGET  # #97

    def test_genuine_crash_fires_on_trip_with_the_record(self, monkeypatch):
        # #110: the on_trip callback in the GENUINE-CRASH branch (the early
        # `return exhausted`) must receive the real crash GovernorTripRecord, not
        # None -- distinct from the shared reroute on_trip (#158) which fires on
        # the route_ineligible / oom paths. This branch returns immediately, so
        # its callback is a separate call site.
        def fake_run(config, sources, *, execution_mode, on_spawn=None, **kw):
            return _crashed_result("ValueError: a genuine unrelated bug")

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)
        received: list[Any] = []
        run_job_with_governor(
            _CFG, _SRC, budget_bytes=_BUDGET, poll_interval_s=0.01, on_trip=received.append
        )
        assert len(received) == 1
        assert isinstance(received[0], GovernorTripRecord)  # None under #110
        assert received[0].trip_kind == "crashed"
        assert received[0].error == "ValueError: a genuine unrelated bug"

    def test_self_oom_trip_classified_and_fields_populated(self, monkeypatch):
        # A child that self-OOMed (kernel kill) WITHOUT the governor's monitor
        # tripping: monitor attached (on_spawn called) but never over
        # threshold. Classifier must read "self_oom", not "governor_kill".
        _no_child(monkeypatch)
        received: list[Any] = []

        def fake_run(config, sources, *, execution_mode, on_spawn=None, **kw):
            if on_spawn is not None:
                on_spawn(1)
            if execution_mode == "full_frame":
                return _oom_killed_result(peak_rss_mb=200.0)
            return _completed_result()

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)

        result = run_job_with_governor(
            _CFG,
            _SRC,
            budget_bytes=_BUDGET,
            poll_interval_s=0.01,
            on_trip=received.append,
        )

        trip = result.trips[0]
        # #132 (`and`->`or`) + #154 ("XXself_oomXX") + #155 ("SELF_OOM"): the
        # exact classifier verdict.
        assert trip.trip_kind == "self_oom"
        assert trip.budget_bytes == _BUDGET  # #141
        assert trip.error == "child killed (SIGKILL)"  # #145
        # #158: the shared reroute callback must receive the ACTUAL record.
        assert len(received) == 1
        assert isinstance(received[0], GovernorTripRecord)
        assert received[0].route == "full_frame"


# ==========================================================================
# `run_job_with_governor` defensive unexpected-outcome guard (#130).
# ==========================================================================


class TestUnexpectedOutcomeGuard:
    def test_unexpected_outcome_raises_named_assertion(self, monkeypatch):
        # #130: the AssertionError message -> None. An IsolatedRunResult whose
        # outcome is outside the 3-value Literal reaches the defensive branch;
        # the raised AssertionError must NAME the offending field, not be a
        # bare `AssertionError(None)`.
        _no_child(monkeypatch)

        def fake_run(config, sources, *, execution_mode, on_spawn=None, **kw):
            if on_spawn is not None:
                on_spawn(1)
            weird = _completed_result()
            object.__setattr__(weird, "outcome", "mystery_outcome")
            return weird

        monkeypatch.setattr(_governor, "run_pipeline_isolated", fake_run)

        with pytest.raises(AssertionError, match="unexpected IsolatedRunResult.outcome"):
            run_job_with_governor(_CFG, _SRC, budget_bytes=_BUDGET, poll_interval_s=0.01)

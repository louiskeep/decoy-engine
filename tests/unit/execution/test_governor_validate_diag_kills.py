"""Mutation-kill tests for the runtime governor's VALIDATION + DIAGNOSTIC
helpers (`decoy_engine.execution._governor._validate_call` and
`_exhausted_diagnostic`).

These pin the two functions' MACHINE-OBSERVABLE contract, kept deliberately
narrow so it does not ossify free-text prose:

  - `_validate_call`: the validation VERDICT (does it raise `ValueError`?) for
    the boundary and type-guard cases the existing `TestCallContractValidation`
    suite does not already cover -- specifically the `bool`-is-not-a-valid-
    number guards (a Python `bool` is an `int`, so `True`/`False` would sail
    through a naive numeric check and be silently treated as `1`/`0`) and the
    smallest-valid-`budget_bytes` boundary.
  - `_exhausted_diagnostic`: the STRUCTURED DATA carried inside the human
    diagnostic -- the observed peak (a number), the budget it exceeded (a
    number), and, for an ineligible rung, that the trip is framed as an
    ELIGIBILITY failure carrying its `trip.error` reason rather than
    mis-rendered as a peak-exceeded. The surrounding explanatory prose
    (leading/trailing sentences, the "unknown"/"; " sentinels, letter-case) is
    NOT contract and is intentionally left unpinned.

Mirrors `tests/unit/execution/test_governor.py` (reaches the underscore
helpers through the `_governor` module, as that file already does for
`_governor._RssMonitor`, `_governor._INELIGIBLE_ROUTE_MARKERS`, etc.).
"""

from __future__ import annotations

import pytest

from decoy_engine.execution import _governor
from decoy_engine.execution._governor import GovernorRoute, GovernorTripRecord

_MB = 1024 * 1024

# A valid baseline call-tuple for `_validate_call`; each test perturbs exactly
# one argument so the verdict under test is unambiguous.
_VALID_LADDER: tuple[GovernorRoute, ...] = ("full_frame",)
_VALID_BUDGET = 100 * _MB
_VALID_FRACTION = 0.93
_VALID_INTERVAL = 1.5


# --------------------------------------------------------------------------
# _validate_call: verdict kills.
# --------------------------------------------------------------------------


class TestValidateCallVerdictKills:
    def test_bool_budget_bytes_is_rejected(self):
        # Kills mutmut_6: `... or not isinstance(..., int) or ...` -> `... and
        # not isinstance(..., int) or ...`. Because a bool IS an int, the `and`
        # form collapses the bool guard to always-False, so `budget_bytes=True`
        # would be accepted and silently used as `1`. The verdict must stay
        # "reject": budget_bytes must be a real positive int, never a bool.
        with pytest.raises(ValueError, match="budget_bytes"):
            _governor._validate_call(_VALID_LADDER, True, _VALID_FRACTION, _VALID_INTERVAL, {})

    def test_smallest_positive_budget_bytes_is_accepted(self):
        # Kills mutmut_9: `budget_bytes <= 0` -> `budget_bytes <= 1`, which would
        # reject a 1-byte budget. 1 is the smallest valid positive int and must
        # pass validation (return None, no raise).
        assert (
            _governor._validate_call(_VALID_LADDER, 1, _VALID_FRACTION, _VALID_INTERVAL, {})  # type: ignore[func-returns-value]
            is None
        )

    def test_bool_hard_threshold_fraction_is_rejected(self):
        # Kills mutmut_12: the `or`->`and` collapse of the bool guard on
        # hard_threshold_fraction. `True` == 1 satisfies `0 < x <= 1`, so
        # without the bool guard it would be accepted and treated as fraction
        # 1.0. The verdict must stay "reject".
        with pytest.raises(ValueError, match="hard_threshold_fraction"):
            _governor._validate_call(_VALID_LADDER, _VALID_BUDGET, True, _VALID_INTERVAL, {})

    def test_bool_poll_interval_is_rejected(self):
        # Kills mutmut_23: the `or`->`and` collapse of the bool guard on
        # poll_interval_s. `True` == 1 > 0 would be accepted as a 1-second
        # cadence without the guard. The verdict must stay "reject".
        with pytest.raises(ValueError, match="poll_interval_s"):
            _governor._validate_call(_VALID_LADDER, _VALID_BUDGET, _VALID_FRACTION, True, {})


# --------------------------------------------------------------------------
# _exhausted_diagnostic: structured-data kills (peak / budget numbers and the
# ineligibility framing + carried error). Prose is not asserted.
# --------------------------------------------------------------------------


class TestExhaustedDiagnosticDataKills:
    def test_governor_kill_trip_carries_observed_peak_and_budget_numbers(self):
        # One governor_kill rung: peak=95.0MB observed against a 100.0MB budget
        # (100*MB / (1024*1024) == 100.0 exactly).
        trip = GovernorTripRecord(
            route="full_frame",
            budget_bytes=_VALID_BUDGET,
            observed_peak_mb=95.0,
            trip_kind="governor_kill",
            reroute_to="out_of_core",
            error=None,
        )
        out = _governor._exhausted_diagnostic((trip,), _VALID_BUDGET)

        # Kills mutmut_15 (`peak = None`) and mutmut_16 (`is not None` ->
        # `is None`, which drops a real peak to "unknown"): the observed peak
        # NUMBER must be rendered.
        assert "peak=95.0MB" in out
        # Kills mutmut_2/3/4/5 (the `budget_bytes / (1024*1024)` arithmetic
        # mutations): the budget the peak exceeded must be the correct MB
        # figure, not an off-by-scale or astronomically-wrong number.
        assert "exceeded the 100.0MB budget" in out

    def test_route_ineligible_trip_is_framed_as_ineligible_and_carries_its_error(self):
        # An ineligible rung must be described as an ELIGIBILITY failure that
        # carries its trip.error reason -- not mis-routed into the peak-exceeded
        # branch (which drops the error entirely).
        trip = GovernorTripRecord(
            route="out_of_core",
            budget_bytes=_VALID_BUDGET,
            observed_peak_mb=None,
            trip_kind="route_ineligible",
            reroute_to="sequential",
            error="is not out-of-core-eligible",
        )
        out = _governor._exhausted_diagnostic((trip,), _VALID_BUDGET)

        # Kills mutmut_7 (`==` -> `!=`), mutmut_8 (`"XXroute_ineligibleXX"`) and
        # mutmut_9 (`"ROUTE_INELIGIBLE"`): each sends a route_ineligible trip to
        # the else/peak branch, which produces neither the ineligibility framing
        # nor the carried error.
        assert "not eligible for this job" in out
        assert "is not out-of-core-eligible" in out

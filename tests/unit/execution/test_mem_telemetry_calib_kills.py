"""TQ isolated-substrate grade (branch `tq/isolated-substrate-grade`): mutation
kills for the CALIBRATION CORE of `_mem_telemetry.py` -- `recalibrate_k` and
`MemoryTelemetryStore.recalibrate`.

These pin the EXACT machine field each surviving mutant flips: a validation
boundary (`< 0` vs `<= 0`, `>` vs `>=`), the margin-gate arithmetic
(`1 + lower_margin` vs `2 + lower_margin`, strict vs non-strict compare), the
floor-clamped lowering verdict (`suggested < current_k` -> `hold`), the exact
`direction` string, every field carried on each of the four `KRecalibration`
return paths (`path`/`current_k`/`sample_count`/`floor_k`/`percentile` set to
`None` by the mutants), and the store's forwarding of every keyword argument to
`recalibrate_k`.

Free-text `ValueError` message prose (the `percentile`/`floor_k` raise bodies)
is deliberately NOT pinned here: its code path is already pinned by the
substring-`match=` raises in `test_mem_telemetry.py`, and only brittle
full-message-equality could kill a case/wrapper mutation of the prose -- house
style leaves those as accepted non-contract.

Fixtures mirror `test_mem_telemetry.py`.
"""

from __future__ import annotations

import pytest

from decoy_engine.execution._mem_estimate import ExecutionPath, route_intercept_bytes
from decoy_engine.execution._mem_telemetry import (
    MemoryTelemetryRecord,
    MemoryTelemetryStore,
    recalibrate_k,
)

_MB = 1024 * 1024
_GB = 1024 * _MB


def _peak_for_slope(
    slope: float, *, raw_bytes: int = _GB, path: ExecutionPath = "full_frame"
) -> int:
    """`actual_peak_bytes` yielding exactly `slope` as the intercept-removed
    `observed_slope` for `path` -- peak lands on `route intercept + raw * slope`.
    """
    return route_intercept_bytes(path) + int(raw_bytes * slope)


def _record(
    *,
    fingerprint: str = "fp-a",
    path: str = "full_frame",
    raw_bytes: int = _GB,
    actual_peak_bytes: int,
    isolated: bool = True,
    outcome: str = "completed",
    predicted_bytes: int = _GB,
) -> MemoryTelemetryRecord:
    return MemoryTelemetryRecord(
        schema_fingerprint=fingerprint,
        path=path,  # type: ignore[arg-type]
        raw_bytes=raw_bytes,
        predicted_bytes=predicted_bytes,
        actual_peak_bytes=actual_peak_bytes,
        isolated=isolated,
        outcome=outcome,  # type: ignore[arg-type]
    )


def _slope_records(slope: float, n: int, **kw: object) -> list[MemoryTelemetryRecord]:
    return [_record(actual_peak_bytes=_peak_for_slope(slope), **kw) for _ in range(n)]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# recalibrate_k: validation-boundary mutants
# ---------------------------------------------------------------------------


class TestValidationBoundaries:
    def test_lower_margin_zero_is_accepted(self) -> None:
        # `lower_margin == 0` is a legal (zero-headroom) config: the guard is
        # `< 0`, not `<= 0`. The mutant (`<= 0`) rejects 0 and raises.
        result = recalibrate_k([], "full_frame", current_k=3.0, lower_margin=0.0)
        assert result.direction == "hold"
        assert result.sample_count == 0

    def test_floor_equal_to_current_k_is_accepted(self) -> None:
        # A floor EQUAL to the constant in force is legal (a floor strictly
        # above it is the contradiction). The guard is `>`, not `>=`; the mutant
        # (`>=`) rejects floor == current_k and raises.
        result = recalibrate_k([], "full_frame", current_k=1.5, floor_k=1.5)
        assert result.floor_k == 1.5
        assert result.direction == "hold"


# ---------------------------------------------------------------------------
# recalibrate_k: the lowering margin-gate arithmetic and verdict
# ---------------------------------------------------------------------------


class TestLoweringMarginGate:
    def test_margin_multiplier_is_one_plus_lower_margin(self) -> None:
        # margin gate is `slope * (1 + lower_margin) < current_k`. With slope
        # 1.5, current_k 2.5, default margin 0.15: 1.5*1.15 = 1.725 < 2.5 -> the
        # lowering clears and lands at 1.5. The mutant (`2 + lower_margin`) makes
        # it 1.5*2.15 = 3.225, which does NOT clear, so it would hold at 2.5.
        records = _slope_records(1.5, 25)
        result = recalibrate_k(records, "full_frame", current_k=2.5, floor_k=0.5)
        assert result.direction == "lower"
        assert result.suggested_k == pytest.approx(1.5)

    def test_margin_comparison_is_strict(self) -> None:
        # The gate is a STRICT `<`. Tuned so `slope * (1 + lower_margin)` hits
        # current_k EXACTLY: slope 2.0, lower_margin 0.25 -> 2.0*1.25 = 2.5 ==
        # current_k. `2.5 < 2.5` is False -> hold at 2.5. The mutant (`<=`) would
        # treat equality as cleared and lower to 2.0.
        records = _slope_records(2.0, 25)
        result = recalibrate_k(records, "full_frame", current_k=2.5, floor_k=0.5, lower_margin=0.25)
        assert result.direction == "hold"
        assert result.suggested_k == pytest.approx(2.5)

    def test_floor_clamped_lowering_holds_when_suggestion_equals_current_k(self) -> None:
        # Gates pass, but the safety-bound clamp lifts the suggestion back to
        # current_k (floor == current_k == 1.5, observed slope 1.0). The verdict
        # is then `hold` because `suggested < current_k` is False. Kills the
        # `<=` boundary mutant AND both string mutations of the `"hold"` literal.
        records = _slope_records(1.0, 25)
        result = recalibrate_k(records, "full_frame", current_k=1.5, floor_k=1.5)
        assert result.gates_passed is True
        assert result.suggested_k == pytest.approx(1.5)
        assert result.direction == "hold"


# ---------------------------------------------------------------------------
# recalibrate_k: every field carried on each of the four return paths.
# The mutants set path/current_k/sample_count/floor_k/percentile to None; each
# test reaches one path and pins the exact value the field must carry.
# ---------------------------------------------------------------------------


class TestReturnPathFieldIntegrity:
    def test_zero_sample_path_carries_all_fields(self) -> None:
        # sample_count == 0 early return.
        result = recalibrate_k([], "full_frame", current_k=3.0)
        assert result.path == "full_frame"
        assert result.current_k == 3.0
        assert result.floor_k == 1.5  # _K_FLOOR_DEFAULT["full_frame"]
        assert result.percentile == 1.0

    def test_raise_path_carries_all_fields(self) -> None:
        result = recalibrate_k(
            [_record(actual_peak_bytes=_peak_for_slope(10.0))],
            "full_frame",
            current_k=3.0,
            floor_k=1.5,
        )
        assert result.direction == "raise"
        assert result.path == "full_frame"
        assert result.current_k == 3.0
        assert result.floor_k == 1.5
        assert result.percentile == 1.0

    def test_lowering_gate_failed_path_carries_all_fields(self) -> None:
        # 3 samples < min_samples_for_lower (20): a lowering is rejected outright.
        records = _slope_records(0.5, 3)
        result = recalibrate_k(records, "full_frame", current_k=3.0, floor_k=1.0)
        assert result.direction == "hold"
        assert result.gates_passed is False
        assert result.path == "full_frame"
        assert result.current_k == 3.0
        assert result.floor_k == 1.0
        assert result.percentile == 1.0

    def test_lowering_applied_path_carries_all_fields(self) -> None:
        records = _slope_records(1.0, 25)
        result = recalibrate_k(records, "full_frame", current_k=3.0, floor_k=0.5)
        assert result.direction == "lower"
        assert result.path == "full_frame"
        assert result.current_k == 3.0
        assert result.percentile == 1.0

    def test_equal_slope_path_carries_all_fields(self) -> None:
        # percentile_slope == current_k exactly -> the terminal hold return.
        records = _slope_records(3.0, 5)
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.direction == "hold"
        assert result.gates_passed is True
        assert result.path == "full_frame"
        assert result.current_k == 3.0
        assert result.sample_count == 5
        assert result.floor_k == 1.5
        assert result.percentile == 1.0


# ---------------------------------------------------------------------------
# MemoryTelemetryStore.recalibrate: forwards every keyword argument.
# The mutants drop / null one argument in the delegated recalibrate_k call.
# ---------------------------------------------------------------------------


class TestStoreForwardsEveryArgument:
    def test_forwards_floor_k(self) -> None:
        # floor_k=0.5 lets the lowering land at the observed slope 1.0. Dropped
        # (mutant), the callee default (None -> 1.5) clamps it up instead.
        store = MemoryTelemetryStore()
        for record in _slope_records(1.0, 25):
            store.add(record)
        result = store.recalibrate("full_frame", current_k=3.0, floor_k=0.5)
        assert result.floor_k == 0.5
        assert result.suggested_k == pytest.approx(1.0)

    def test_forwards_schema_fingerprint(self) -> None:
        # Scope to fp-b only: the fp-a slope-10 outlier must not leak in.
        store = MemoryTelemetryStore()
        store.add(_record(fingerprint="fp-a", actual_peak_bytes=_peak_for_slope(10.0)))
        store.add(_record(fingerprint="fp-b", actual_peak_bytes=_peak_for_slope(1.0)))
        result = store.recalibrate(
            "full_frame", current_k=3.0, schema_fingerprint="fp-b", floor_k=0.5
        )
        assert result.sample_count == 1
        assert result.direction != "raise"

    def test_forwards_min_samples_for_lower(self) -> None:
        # min_samples_for_lower=1 lets 5 samples lower; the callee default (20)
        # would hold. Dropped (mutant), the default holds instead of lowering.
        store = MemoryTelemetryStore()
        for record in _slope_records(1.0, 5):
            store.add(record)
        result = store.recalibrate(
            "full_frame", current_k=3.0, floor_k=0.5, min_samples_for_lower=1
        )
        assert result.direction == "lower"
        assert result.suggested_k == pytest.approx(1.0)

    def test_forwards_percentile(self) -> None:
        # percentile=0.99 must be forwarded and rejected; dropped, the callee
        # default (1.0) is used and NO error is raised.
        store = MemoryTelemetryStore()
        store.add(_record(actual_peak_bytes=_peak_for_slope(10.0)))
        with pytest.raises(ValueError, match="percentile"):
            store.recalibrate("full_frame", current_k=3.0, percentile=0.99)

    def test_forwards_lower_margin(self) -> None:
        # lower_margin=-0.1 must be forwarded and rejected; dropped, the callee
        # default (0.15) is used and NO error is raised.
        store = MemoryTelemetryStore()
        with pytest.raises(ValueError, match="lower_margin"):
            store.recalibrate("full_frame", current_k=3.0, lower_margin=-0.1)

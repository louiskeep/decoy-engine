"""TB-5 precondition (issue #72): acceptance test for the intercept model.

`docs/plans/2026-07-12-track-b-completion-program.md` TB-4/TB-5;
`docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §13. TB-4 pinned
MEASURED per-route SLOPES but modeled THROUGH-ORIGIN (`basis * slope`), which
OMITS the fixed baseline-RSS intercept (~200 MB in-core, ~450 MB out_of_core)
and therefore UNDER-predicts SMALL-basis jobs -- the OOM-unsafe direction (a
job admitted that then OOMs). This module now predicts
`intercept + basis * slope`. These tests prove:

  * FAIL-PRE: the old through-origin model under-predicted small-basis peaks,
    specifically numeric_fk full_frame @500k (767 MB predicted < 858 MB real).
  * PASS-POST: the intercept model is CONSERVATIVE (predicted >= observed) at
    BOTH the SMALL and LARGE measured points for EVERY route -- the same 858 MB
    point is now covered (976 MB predicted).
  * The intercept never LOWERS a prediction (it only raises the small-basis
    floor), and the slopes still sit within `K_CALIBRATION_ERROR_BAND` above the
    measured worst-case slope for the tightly-modeled routes.

The measured pairs below are the exact isolated-run VmHWM measurements from the
TB-4 sweep (`docs/plans/2026-07-13-tb4-calibration-results.md`), each at TWO row
scales per (shape, route). A two-point SLOPE `k = dpeak / dbasis` cancels the
fixed intercept; the INTERCEPT is that same baseline fitted back out.
"""

from __future__ import annotations

import inspect

import pytest

from decoy_engine.execution._mem_estimate import (
    K_CALIBRATION_ERROR_BAND,
    K_FULL_FRAME_SLOPE,
    K_INTERCEPT_BYTES,
    K_OUT_OF_CORE_INTERCEPT_BYTES,
    K_OUT_OF_CORE_SLOPE,
    K_SEQUENTIAL_SLOPE,
    ColumnSizeSpec,
    ExecutionPath,
    TableSizeSpec,
    estimate_peak_bytes,
    raw_data_bytes,
)

# (shape, route, rows, basis_bytes, peak_bytes) -- the exact TB-4 sweep points.
_MEASURED: tuple[tuple[str, ExecutionPath, int, int, int], ...] = (
    ("pooled_fk", "full_frame", 400_000, 316_466_670, 871_471_513),
    ("pooled_fk", "out_of_core", 400_000, 316_466_670, 499_017_318),
    ("pooled_fk", "sequential", 400_000, 316_466_670, 744_279_244),
    ("pooled_fk", "full_frame", 800_000, 633_266_670, 1_538_470_707),
    ("pooled_fk", "out_of_core", 800_000, 633_266_670, 621_595_852),
    ("pooled_fk", "sequential", 800_000, 633_266_670, 1_267_413_811),
    ("numeric_fk", "full_frame", 500_000, 191_666_670, 858_049_740),
    ("numeric_fk", "out_of_core", 500_000, 191_666_670, 628_621_312),
    ("numeric_fk", "sequential", 500_000, 191_666_670, 799_644_057),
    ("numeric_fk", "full_frame", 1_000_000, 383_666_670, 1_519_910_912),
    ("numeric_fk", "out_of_core", 1_000_000, 383_666_670, 810_968_678),
    ("numeric_fk", "sequential", 1_000_000, 383_666_670, 1_428_789_657),
    ("numeric_single", "full_frame", 1_000_000, 160_000_000, 684_195_840),
    ("numeric_single", "full_frame", 2_000_000, 320_000_000, 1_168_847_667),
    ("unique_single", "full_frame", 1_000_000, 73_000_000, 377_592_217),
    ("unique_single", "full_frame", 2_000_000, 146_000_000, 554_801_561),
)

# The pinned per-route slope + intercept the module applies (peak = I + b*s).
_SLOPE: dict[ExecutionPath, float] = {
    "full_frame": K_FULL_FRAME_SLOPE,
    "out_of_core": K_OUT_OF_CORE_SLOPE,
    "sequential": K_SEQUENTIAL_SLOPE,
}
_INTERCEPT: dict[ExecutionPath, int] = {
    "full_frame": K_INTERCEPT_BYTES,
    "out_of_core": K_OUT_OF_CORE_INTERCEPT_BYTES,
    "sequential": K_INTERCEPT_BYTES,
}

# The pre-TB-4 placeholder constants (design §13, pre-calibration).
_OLD_PLACEHOLDER: dict[ExecutionPath, float] = {
    "full_frame": 3.0,
    "out_of_core": 2.0,
    "sequential": 1.5,
}

# The key point the fix targets: numeric_fk full_frame @500k -- the small-basis
# case the through-origin model under-predicted (767 MB) below its real 858 MB.
_NUMERIC_FK_FF_500K = next(
    pt for pt in _MEASURED if pt[:3] == ("numeric_fk", "full_frame", 500_000)
)


def _through_origin(route: ExecutionPath, basis: int) -> float:
    """The OLD (TB-4) model: basis * slope, with NO intercept."""
    return basis * _SLOPE[route]


def _intercept_model(route: ExecutionPath, basis: int) -> float:
    """The NEW model: intercept + basis * slope."""
    return _INTERCEPT[route] + basis * _SLOPE[route]


def _slope_by_route() -> dict[ExecutionPath, float]:
    """Max two-point slope k per route across all sampled shapes -- the
    intercept-free per-byte multiplier the slope constant must cover."""
    points: dict[tuple[str, ExecutionPath], dict[int, tuple[int, int]]] = {}
    for shape, route, rows, basis, peak in _MEASURED:
        points.setdefault((shape, route), {})[rows] = (basis, peak)
    max_slope: dict[ExecutionPath, float] = {}
    for (_shape, route), by_rows in points.items():
        scales = sorted(by_rows)
        lo, hi = by_rows[scales[0]], by_rows[scales[-1]]
        slope = (hi[1] - lo[1]) / (hi[0] - lo[0])
        max_slope[route] = max(max_slope.get(route, 0.0), slope)
    return max_slope


def _numeric_fk_fit() -> dict[ExecutionPath, tuple[float, float]]:
    """Per-route (slope, intercept) two-point fit from the numeric_fk shape --
    the no-pooling WORST-case per-byte slope the constants are pinned against
    (`raw_data_bytes` prices it exactly). intercept = peak_low - slope*basis_low.
    Other shapes with lower slopes fit a HIGHER intercept (their peak mass moves
    from the per-byte term to the fixed term), but the pinned high slope covers
    them -- conservatism at every point is proven in
    `TestInterceptModelIsConservativeEverywhere`, not by intercept alone."""
    by_route: dict[ExecutionPath, dict[int, tuple[int, int]]] = {}
    for shape, route, rows, basis, peak in _MEASURED:
        if shape == "numeric_fk":
            by_route.setdefault(route, {})[rows] = (basis, peak)
    fit: dict[ExecutionPath, tuple[float, float]] = {}
    for route, by_rows in by_route.items():
        scales = sorted(by_rows)
        lo, hi = by_rows[scales[0]], by_rows[scales[-1]]
        slope = (hi[1] - lo[1]) / (hi[0] - lo[0])
        intercept = lo[1] - slope * lo[0]
        fit[route] = (slope, intercept)
    return fit


def _asymptotic_points() -> list[tuple[str, ExecutionPath, int, int, int]]:
    """The LARGER row-scale point per (shape, route) -- the regime where basis
    dominates the intercept and a large-job OOM would actually occur."""
    by_key: dict[tuple[str, ExecutionPath], tuple[int, int, int]] = {}
    for shape, route, rows, basis, peak in _MEASURED:
        key = (shape, route)
        if key not in by_key or rows > by_key[key][0]:
            by_key[key] = (rows, basis, peak)
    return [(s, r, rows, b, p) for (s, r), (rows, b, p) in by_key.items()]


class TestMeasuredModelIsLinear:
    """The `intercept + basis * slope` model's premise: peak is LINEAR in basis
    (a stable POSITIVE intercept + per-byte slope). If this failed, TB-4's
    STOP-and-report clause would fire and the intercept fit would be invalid."""

    @pytest.mark.parametrize("shape,route", sorted({(s, r) for s, r, *_ in _MEASURED}))
    def test_peak_is_linear_in_basis_positive_slope_and_positive_intercept(
        self, shape: str, route: ExecutionPath
    ) -> None:
        pts = sorted(
            (basis, peak) for s, r, _rows, basis, peak in _MEASURED if s == shape and r == route
        )
        (b_lo, p_lo), (b_hi, p_hi) = pts[0], pts[-1]
        slope = (p_hi - p_lo) / (b_hi - b_lo)
        intercept = p_lo - slope * b_lo
        # A real per-byte cost (slope > 0) on top of a POSITIVE fixed baseline
        # (interpreter/pyarrow/DuckDB) -- the two terms the fit separates.
        assert slope > 0
        assert intercept > 0


class TestOldPlaceholdersWereOff:
    """The pre-TB-4 placeholder SLOPES diverged from the measured peaks; two
    diverged in the OOM-UNSAFE direction (below the measured worst case)."""

    def test_full_frame_old_3_0_under_predicts_the_numeric_worst_case(self) -> None:
        assert _slope_by_route()["full_frame"] > _OLD_PLACEHOLDER["full_frame"]

    def test_sequential_old_1_5_under_predicts_the_numeric_worst_case(self) -> None:
        assert _slope_by_route()["sequential"] > _OLD_PLACEHOLDER["sequential"]

    def test_out_of_core_old_2_0_diverged_from_the_measured_slope(self) -> None:
        slope = _slope_by_route()["out_of_core"]
        assert slope < 1.0
        assert _OLD_PLACEHOLDER["out_of_core"] > slope * (1 + K_CALIBRATION_ERROR_BAND)


class TestThroughOriginModelUnderPredictedSmallBasis:
    """FAIL-PRE: the OLD through-origin model (`basis * slope`, no intercept)
    under-predicted small-basis peaks -- the gap the intercept closes."""

    def test_through_origin_under_predicts_numeric_fk_full_frame_500k(self) -> None:
        # The canonical case from the design/issue: 767 MB predicted < 858 MB.
        _s, route, _rows, basis, peak = _NUMERIC_FK_FF_500K
        predicted = _through_origin(route, basis)
        assert predicted < peak
        # Pin the numbers the issue names, in decimal MB, to catch drift.
        assert round(predicted / 1_000_000) == 767
        assert round(peak / 1_000_000) == 858

    def test_through_origin_under_predicts_at_least_one_small_point_per_route(self) -> None:
        # Every route has a SMALL-basis measured point the through-origin model
        # under-priced -- the omitted intercept, present on all three routes.
        small_by_route: dict[ExecutionPath, tuple[int, int]] = {}
        for _s, route, _rows, basis, peak in _MEASURED:
            if route not in small_by_route or basis < small_by_route[route][0]:
                small_by_route[route] = (basis, peak)
        for route, (basis, peak) in small_by_route.items():
            assert _through_origin(route, basis) < peak, (
                f"{route}: through-origin should under-predict its smallest point"
            )


class TestInterceptModelIsConservativeEverywhere:
    """PASS-POST: the intercept model predicts >= observed peak at BOTH the
    SMALL and LARGE measured points for EVERY route (the core acceptance)."""

    @pytest.mark.parametrize("shape,route,rows,basis,peak", _MEASURED)
    def test_predicted_at_least_observed_at_every_measured_point(
        self, shape: str, route: ExecutionPath, rows: int, basis: int, peak: int
    ) -> None:
        predicted = _intercept_model(route, basis)
        assert predicted >= peak, (
            f"{shape}/{route}@{rows}: predicted {predicted} < observed {peak} -- "
            "the intercept model UNDER-predicts (OOM-unsafe)."
        )

    def test_numeric_fk_full_frame_500k_gap_is_now_covered(self) -> None:
        # The exact fail-pre point (767 < 858) is now covered (976 >= 858).
        _s, route, _rows, basis, peak = _NUMERIC_FK_FF_500K
        predicted = _intercept_model(route, basis)
        assert predicted >= peak
        assert round(predicted / 1_000_000) == 976

    def test_conservative_at_scale_for_every_route(self) -> None:
        # The asymptotic (large-basis) regime where an OOM would occur stays
        # conservative for every sampled (shape, route).
        for shape, route, rows, basis, peak in _asymptotic_points():
            assert _intercept_model(route, basis) >= peak, (
                f"{shape}/{route}@{rows}: under-predicts at scale"
            )

    def test_out_of_core_is_now_conservative_not_governor_only(self) -> None:
        # BEFORE the intercept, out_of_core's ~450 MB fixed floor made a
        # through-origin `basis * slope` under-predict its measured points (the
        # runtime budget + governor were its only bound). The per-route
        # intercept now covers that floor, so out_of_core is ALSO conservative
        # at both measured scales -- while the governor (TB-1/TB-2/TB-3) remains
        # the real RUNTIME bound.
        oc = [pt for pt in _MEASURED if pt[1] == "out_of_core"]
        assert oc
        # At least one out_of_core point WAS under-predicted through-origin (the
        # omitted floor is real), and ALL are now covered by the intercept.
        assert any(_through_origin(r, b) < p for _s, r, _rows, b, p in oc)
        for shape, route, rows, basis, peak in oc:
            assert _intercept_model(route, basis) >= peak, f"{shape}@{rows}"


class TestInterceptNeverLowersAPrediction:
    """The intercept only RAISES predictions (it is strictly positive) -- no
    route's prediction drops below the old through-origin model at any size, so
    the fix cannot re-introduce an OOM by loosening a previously-safe route."""

    @pytest.mark.parametrize("shape,route,rows,basis,peak", _MEASURED)
    def test_intercept_model_never_below_through_origin(
        self, shape: str, route: ExecutionPath, rows: int, basis: int, peak: int
    ) -> None:
        assert _intercept_model(route, basis) >= _through_origin(route, basis)


class TestSlopesAreCalibratedNotArbitrary:
    """The tightly-modeled routes (modest intercept: full_frame, sequential)
    keep a SLOPE within `K_CALIBRATION_ERROR_BAND` above their measured
    worst-case slope -- conservative growth, but not wastefully loose."""

    @pytest.mark.parametrize("route", ["full_frame", "sequential"])
    def test_slope_within_error_band_above_measured_slope(self, route: ExecutionPath) -> None:
        slope = _slope_by_route()[route]
        pinned = _SLOPE[route]
        assert slope <= pinned <= slope * (1 + K_CALIBRATION_ERROR_BAND)

    def test_out_of_core_slope_is_conservatively_above_its_slope(self) -> None:
        assert _SLOPE["out_of_core"] > _slope_by_route()["out_of_core"]

    def test_pinned_constants_match_the_numeric_fk_worst_case_fit(self) -> None:
        # The pinned slope+intercept per route are >= the numeric_fk two-point
        # fit they are derived from (rounded UP), and out_of_core's intercept is
        # materially larger than the shared in-core baseline (~2.3x): it runs
        # DuckDB + a budget-bounded buffer, not just the interpreter floor.
        fit = _numeric_fk_fit()
        for route in ("full_frame", "out_of_core", "sequential"):
            slope, intercept = fit[route]
            assert _SLOPE[route] >= slope, route
            assert _INTERCEPT[route] >= intercept, route
        assert _INTERCEPT["out_of_core"] > _INTERCEPT["full_frame"] * 2


class TestEstimatorEndToEndUsesTheInterceptModel:
    """`estimate_peak_bytes` applies `intercept + basis * slope`, and the
    resulting estimate covers the numeric-FK worst case at the measured scale."""

    def test_full_frame_estimate_is_intercept_plus_basis_times_slope(self) -> None:
        peak = next(
            p
            for s, r, rows, _b, p in _MEASURED
            if s == "numeric_fk" and r == "full_frame" and rows == 1_000_000
        )
        rows = 1_000_000
        key_w = 7.0  # ~avg len of "p<i>" over 1M rows
        payload = tuple(ColumnSizeSpec(f"v{i}", "int64") for i in range(12))
        parent = TableSizeSpec(
            "parent", rows, (ColumnSizeSpec("id", "object", string_width_bytes=key_w), *payload)
        )
        child = TableSizeSpec(
            "child",
            rows,
            (
                ColumnSizeSpec("cid", "object", string_width_bytes=key_w),
                ColumnSizeSpec("pid", "object", string_width_bytes=key_w),
                *payload,
            ),
        )
        raw = raw_data_bytes((parent, child))
        estimate = estimate_peak_bytes((parent, child), "full_frame")
        assert not estimate.unpriceable
        assert estimate.estimated_bytes == int(
            K_INTERCEPT_BYTES + raw.priceable_bytes * K_FULL_FRAME_SLOPE
        )
        # Conservative against the real measured peak at this scale.
        assert estimate.estimated_bytes >= peak

    def test_small_basis_estimate_now_clears_the_numeric_fk_500k_peak(self) -> None:
        # End-to-end at the fail-pre scale: reconstruct the numeric_fk@500k
        # estimator basis and confirm the full estimate now clears 858 MB.
        _s, _r, _rows, _basis, peak = _NUMERIC_FK_FF_500K
        rows = 500_000
        key_w = 6.0  # ~avg len of "p<i>" over 500k rows
        payload = tuple(ColumnSizeSpec(f"v{i}", "int64") for i in range(12))
        parent = TableSizeSpec(
            "parent", rows, (ColumnSizeSpec("id", "object", string_width_bytes=key_w), *payload)
        )
        child = TableSizeSpec(
            "child",
            rows,
            (
                ColumnSizeSpec("cid", "object", string_width_bytes=key_w),
                ColumnSizeSpec("pid", "object", string_width_bytes=key_w),
                *payload,
            ),
        )
        estimate = estimate_peak_bytes((parent, child), "full_frame")
        assert estimate.estimated_bytes is not None
        assert estimate.estimated_bytes >= peak


class TestByteEstimateRoutingStaysDefaultOff:
    """This is a VALUES/model change behind the flag-gated estimate path -- no
    live routing change. The estimate only feeds routing when the operator
    opts in; the flag default must stay OFF (it flips at TB-5, not here)."""

    def test_use_byte_estimate_routing_default_is_false(self) -> None:
        from decoy_engine.execution._pipeline import run_pipeline
        from decoy_engine.execution._pipeline_routing import decide_execution_route

        assert (
            inspect.signature(run_pipeline).parameters["use_byte_estimate_routing"].default is False
        )
        assert (
            inspect.signature(decide_execution_route)
            .parameters["use_byte_estimate_routing"]
            .default
            is False
        )

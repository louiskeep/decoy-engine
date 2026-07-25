"""Sprint B5: the TELEMETRY self-calibration loop (`_mem_telemetry.py`).
OOM-avoidance routing redesign, `docs/plans/2026-07-10-oom-avoidance-routing-
redesign.md` §3.4, corrected by §11's §3.4 erratum and refined by §13.

These tests pin the safety property, not just the arithmetic: `recalibrate_k`
must never produce a `k` that under-shoots a real observed peak.
`schema_fingerprint` must aggregate the same shape across row counts and
split on anything that changes the shape. The two emission helpers must
refuse to manufacture a record from data that is not trustworthy evidence
(an un-reported peak, or a non-memory governor trip).
"""

from __future__ import annotations

import pytest

from decoy_engine.execution._governor import GovernorTripRecord
from decoy_engine.execution._isolated_common import IsolatedRunResult
from decoy_engine.execution._mem_estimate import (
    K_CALIBRATION_ERROR_BAND,
    K_FULL_FRAME_SLOPE,
    K_INTERCEPT_BYTES,
    K_OUT_OF_CORE_INTERCEPT_BYTES,
    ColumnSizeSpec,
    ExecutionPath,
    TableSizeSpec,
    estimator_basis_bytes,
    route_intercept_bytes,
)
from decoy_engine.execution._mem_telemetry import (
    KRecalibration,
    MemoryTelemetryRecord,
    MemoryTelemetryStore,
    recalibrate_k,
    schema_fingerprint,
    telemetry_record_from_governor_trip,
    telemetry_record_from_isolated_run,
)

_MB = 1024 * 1024
_GB = 1024 * _MB


def _table(name: str, row_count: int, columns: tuple[ColumnSizeSpec, ...]) -> TableSizeSpec:
    return TableSizeSpec(name=name, row_count=row_count, columns=columns)


def _peak_for_slope(
    slope: float, *, raw_bytes: int = 1_000_000_000, path: ExecutionPath = "full_frame"
) -> int:
    """`actual_peak_bytes` that yields exactly `slope` as the intercept-removed
    `observed_slope` for `path` -- i.e. a job whose peak lands on the model
    `route intercept + raw_bytes * slope`. Lets a test say "observed slope 5.0"
    without hand-mixing the per-route intercept into the peak.
    """
    return route_intercept_bytes(path) + int(raw_bytes * slope)


def _record(
    *,
    fingerprint: str = "fp-a",
    path: str = "full_frame",
    raw_bytes: int = 1_000_000_000,
    actual_peak_bytes: int,
    isolated: bool = True,
    outcome: str = "completed",
    predicted_bytes: int = 1_000_000_000,
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


# ---------------------------------------------------------------------------
# schema_fingerprint
# ---------------------------------------------------------------------------


class TestSchemaFingerprint:
    def test_same_shape_different_row_counts_fingerprints_identically(self) -> None:
        cols = (
            ColumnSizeSpec(name="id", dtype="int64"),
            ColumnSizeSpec(name="name", dtype="object", string_width_bytes=12.0),
        )
        small = (_table("t1", 1_000, cols),)
        large = (_table("t1", 5_000_000, cols),)
        assert schema_fingerprint(small) == schema_fingerprint(large)

    def test_different_dtype_changes_fingerprint(self) -> None:
        base = (_table("t1", 1_000, (ColumnSizeSpec(name="id", dtype="int64"),)),)
        changed = (_table("t1", 1_000, (ColumnSizeSpec(name="id", dtype="float64"),)),)
        assert schema_fingerprint(base) != schema_fingerprint(changed)

    def test_different_string_width_class_changes_fingerprint(self) -> None:
        narrow = (
            _table(
                "t1", 1_000, (ColumnSizeSpec(name="s", dtype="object", string_width_bytes=10.0),)
            ),
        )
        wide = (
            _table(
                "t1", 1_000, (ColumnSizeSpec(name="s", dtype="object", string_width_bytes=200.0),)
            ),
        )
        assert schema_fingerprint(narrow) != schema_fingerprint(wide)

    def test_same_width_class_does_not_change_fingerprint(self) -> None:
        # 10 and 14 both fall in the same "<=16" bucket -- sampling noise
        # within a bucket must not fragment the fingerprint.
        a = (
            _table(
                "t1", 1_000, (ColumnSizeSpec(name="s", dtype="object", string_width_bytes=10.0),)
            ),
        )
        b = (
            _table(
                "t1", 9_000, (ColumnSizeSpec(name="s", dtype="object", string_width_bytes=14.0),)
            ),
        )
        assert schema_fingerprint(a) == schema_fingerprint(b)

    def test_different_table_count_changes_fingerprint(self) -> None:
        cols = (ColumnSizeSpec(name="id", dtype="int64"),)
        one_table = (_table("t1", 1_000, cols),)
        two_tables = (_table("t1", 1_000, cols), _table("t2", 1_000, cols))
        assert schema_fingerprint(one_table) != schema_fingerprint(two_tables)

    def test_different_fk_structure_changes_fingerprint(self) -> None:
        cols = (ColumnSizeSpec(name="id", dtype="int64"),)
        tables = (_table("parent", 1_000, cols), _table("child", 1_000, cols))
        no_fk = schema_fingerprint(tables)
        with_fk = schema_fingerprint(tables, fk_edges=(("child", "parent"),))
        assert no_fk != with_fk

    def test_unknown_fk_table_name_raises(self) -> None:
        cols = (ColumnSizeSpec(name="id", dtype="int64"),)
        tables = (_table("parent", 1_000, cols),)
        with pytest.raises(ValueError, match="not present"):
            schema_fingerprint(tables, fk_edges=(("child", "parent"),))

    def test_unpriceable_column_participates_in_the_fingerprint(self) -> None:
        priceable = (
            _table(
                "t1", 1_000, (ColumnSizeSpec(name="s", dtype="object", string_width_bytes=10.0),)
            ),
        )
        unpriceable = (
            _table("t1", 1_000, (ColumnSizeSpec(name="s", dtype="object", unpriceable=True),)),
        )
        assert schema_fingerprint(priceable) != schema_fingerprint(unpriceable)


# ---------------------------------------------------------------------------
# MemoryTelemetryRecord
# ---------------------------------------------------------------------------


class TestMemoryTelemetryRecord:
    def test_observed_k_is_actual_over_raw(self) -> None:
        record = _record(raw_bytes=2_000_000_000, actual_peak_bytes=6_000_000_000)
        assert record.observed_k == pytest.approx(3.0)

    def test_observed_slope_removes_the_route_intercept(self) -> None:
        # peak = full_frame intercept + raw * 3.0 -> observed_slope is exactly
        # 3.0, while the raw point ratio observed_k reads HIGHER because the
        # intercept has not been subtracted.
        record = _record(raw_bytes=_GB, actual_peak_bytes=_peak_for_slope(3.0, raw_bytes=_GB))
        assert record.observed_slope == pytest.approx(3.0)
        assert record.observed_k > record.observed_slope  # intercept inflation

    def test_observed_slope_uses_the_per_route_intercept(self) -> None:
        # The SAME peak/basis reads a lower slope on out_of_core than on
        # full_frame, because out_of_core's fixed intercept is larger.
        peak = 5 * _GB
        ff = _record(raw_bytes=_GB, actual_peak_bytes=peak, path="full_frame")
        ooc = _record(raw_bytes=_GB, actual_peak_bytes=peak, path="out_of_core")
        assert ff.observed_slope > ooc.observed_slope
        assert ff.observed_slope == pytest.approx((peak - K_INTERCEPT_BYTES) / _GB)
        assert ooc.observed_slope == pytest.approx((peak - K_OUT_OF_CORE_INTERCEPT_BYTES) / _GB)

    def test_observed_slope_floors_at_zero_below_the_intercept(self) -> None:
        # A whole peak that fits under the fixed intercept shows no per-byte
        # growth at all -- a negative slope is not a meaningful drift signal.
        record = _record(raw_bytes=_GB, actual_peak_bytes=K_INTERCEPT_BYTES // 2)
        assert record.observed_slope == 0.0

    def test_zero_raw_bytes_rejected(self) -> None:
        with pytest.raises(ValueError, match="raw_bytes"):
            _record(raw_bytes=0, actual_peak_bytes=1)

    def test_negative_actual_peak_rejected(self) -> None:
        with pytest.raises(ValueError, match="actual_peak_bytes"):
            _record(actual_peak_bytes=-1)

    def test_negative_predicted_bytes_rejected(self) -> None:
        with pytest.raises(ValueError, match="predicted_bytes"):
            _record(actual_peak_bytes=1, predicted_bytes=-1)


# ---------------------------------------------------------------------------
# recalibrate_k -- the safety-critical part
# ---------------------------------------------------------------------------


class TestRecalibrateFiltersNonIsolated:
    def test_non_isolated_records_are_ignored_entirely(self) -> None:
        # A huge non-isolated (contaminated) k must NOT move the suggestion
        # at all -- not even to raise it. If it leaked through, this would
        # raise; it must instead report zero samples.
        records = [
            _record(actual_peak_bytes=50 * _GB, isolated=False),
            _record(actual_peak_bytes=50 * _GB, isolated=False),
        ]
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.sample_count == 0
        assert result.direction == "hold"
        assert result.suggested_k == 3.0

    def test_mixed_isolated_and_non_isolated_only_counts_isolated(self) -> None:
        records = [
            _record(raw_bytes=1 * _GB, actual_peak_bytes=1 * _GB, isolated=True),
            _record(raw_bytes=1 * _GB, actual_peak_bytes=50 * _GB, isolated=False),
        ]
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.sample_count == 1


class TestHighPercentileAggregation:
    def test_mix_of_low_and_high_slope_recalibrates_near_the_high_end(self) -> None:
        # 19 low observations (slope=1.0) and one high one (slope=5.0). The
        # mean would be ~1.2; a max/high-percentile aggregation must land at
        # the high observation, not be diluted toward the low cluster.
        records = [
            _record(raw_bytes=_GB, actual_peak_bytes=_peak_for_slope(1.0, raw_bytes=_GB))
            for _ in range(19)
        ]
        records.append(
            _record(raw_bytes=_GB, actual_peak_bytes=_peak_for_slope(5.0, raw_bytes=_GB))
        )
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.direction == "raise"
        assert result.suggested_k == pytest.approx(5.0)
        mean_slope = sum(r.observed_slope for r in records) / len(records)
        assert result.suggested_k > mean_slope + 2.0  # nowhere near the diluted mean

    def test_single_high_outlier_keeps_slope_high(self) -> None:
        records = [
            _record(raw_bytes=_GB, actual_peak_bytes=_peak_for_slope(1.0, raw_bytes=_GB))
            for _ in range(99)
        ]
        records.append(
            _record(raw_bytes=_GB, actual_peak_bytes=_peak_for_slope(10.0, raw_bytes=_GB))
        )
        result = recalibrate_k(records, "full_frame", current_k=2.0, floor_k=1.0)
        assert result.direction == "raise"
        assert result.suggested_k == pytest.approx(10.0)

    def test_percentile_one_is_the_exact_maximum(self) -> None:
        observed = [0.5, 0.9, 1.0, 1.1, 4.2]
        records = [
            _record(
                raw_bytes=_GB,
                actual_peak_bytes=_peak_for_slope(s, raw_bytes=_GB, path="out_of_core"),
                path="out_of_core",
            )
            for s in observed
        ]
        result = recalibrate_k(
            records, "out_of_core", current_k=1.0, min_samples_for_lower=1, floor_k=0.1
        )
        assert result.suggested_k == pytest.approx(4.2)


class TestInterceptAwareDriftSignal:
    """TB-5 precondition #72: the drift detector compares an INTERCEPT-REMOVED
    slope against the pinned `K_<route>_SLOPE`, not the raw through-origin
    point ratio. A small-basis job whose peak fits `intercept + basis*slope`
    must NOT spuriously trigger a raise (the old point ratio would have); a
    genuine slope drift still must.
    """

    # numeric_fk full_frame @500k, the shape the #72 gate cited: a real
    # 858 MB peak on a 191.7 MB raw basis. It FITS the model (predicted
    # 200 MiB intercept + 191.7 MB * 4.0 slope ~= 967 MB >= 858 MB), yet its
    # through-origin point ratio (858/191.7 = 4.48) sits ABOVE the 4.0 slope.
    _FITS_RAW_BYTES = int(191.7 * _MB)
    _FITS_PEAK_BYTES = 858 * _MB

    def test_small_basis_job_that_fits_the_model_does_not_spuriously_raise(self) -> None:
        record = _record(
            raw_bytes=self._FITS_RAW_BYTES,
            actual_peak_bytes=self._FITS_PEAK_BYTES,
            path="full_frame",
        )
        current_k = K_FULL_FRAME_SLOPE  # 4.0, the pinned slope

        # The job genuinely fits the estimator's own model (safe over-predict):
        # intercept + basis*slope >= the real peak, so there is nothing to
        # recalibrate toward.
        model_predicted = route_intercept_bytes("full_frame") + int(
            self._FITS_RAW_BYTES * current_k
        )
        assert model_predicted >= self._FITS_PEAK_BYTES  # fits with headroom

        # FAIL-PRE: the OLD through-origin point ratio exceeds the slope, so
        # the pre-#72 detector (comparing observed_k against current_k) WOULD
        # have fired a spurious raise on this fitting job.
        assert record.observed_k > current_k

        # PASS-POST: the intercept-removed slope is BELOW the pinned slope, so
        # the new detector does not.
        assert record.observed_slope < current_k

        result = recalibrate_k([record], "full_frame", current_k=current_k)
        assert result.direction != "raise"  # no spurious raise
        assert result.direction == "hold"  # 1 sample < min_samples_for_lower
        assert result.suggested_k == current_k  # no upward ratchet

    def test_a_stream_of_fitting_small_jobs_never_ratchets_the_slope_up(self) -> None:
        # The pathology #72 describes is a RATCHET: every small job's inflated
        # point ratio nudges the slope up. Post-fix, a whole pool of fitting
        # small jobs holds the slope exactly.
        records = [
            _record(
                raw_bytes=self._FITS_RAW_BYTES,
                actual_peak_bytes=self._FITS_PEAK_BYTES,
                path="full_frame",
            )
            for _ in range(50)
        ]
        # Every one would have tripped the old point-ratio raise.
        assert all(r.observed_k > K_FULL_FRAME_SLOPE for r in records)
        result = recalibrate_k(records, "full_frame", current_k=K_FULL_FRAME_SLOPE, floor_k=1.5)
        assert result.direction != "raise"
        assert result.suggested_k <= K_FULL_FRAME_SLOPE

    def test_genuine_slope_drift_still_triggers_a_raise(self) -> None:
        # A job whose peak grows FASTER per byte than the pinned slope --
        # intercept removed, beyond the error band -- must still raise. slope
        # 6.0 vs a 4.0 pinned slope is 1.5x, well past the 1.30 error band.
        drift_slope = 6.0
        current_k = K_FULL_FRAME_SLOPE
        assert drift_slope > current_k * (1 + K_CALIBRATION_ERROR_BAND)  # genuinely beyond band
        record = _record(
            raw_bytes=_GB,
            actual_peak_bytes=_peak_for_slope(drift_slope, raw_bytes=_GB),
            path="full_frame",
        )
        result = recalibrate_k([record], "full_frame", current_k=current_k)
        assert result.direction == "raise"
        assert result.suggested_k == pytest.approx(drift_slope)
        assert result.suggested_k > current_k

    def test_out_of_core_small_job_uses_its_own_larger_intercept(self) -> None:
        # out_of_core's fixed floor (450 MiB) is larger; a small job that
        # would read a high point ratio there is judged against THAT intercept,
        # not the in-core 200 MiB, so it too does not spuriously raise.
        raw = int(300 * _MB)
        peak = int(900 * _MB)  # point ratio 3.0, but slope after 450 MiB removed = 1.5
        record = _record(raw_bytes=raw, actual_peak_bytes=peak, path="out_of_core")
        assert record.observed_k == pytest.approx(peak / raw)  # ~3.0 point ratio
        result = recalibrate_k([record], "out_of_core", current_k=1.5)
        assert record.observed_slope == pytest.approx((peak - K_OUT_OF_CORE_INTERCEPT_BYTES) / raw)
        assert result.direction != "raise"


class TestGovernorTripsPushKUp:
    def test_governor_trip_record_forces_a_raise(self) -> None:
        trip = GovernorTripRecord(
            route="full_frame",
            budget_bytes=8 * _GB,
            observed_peak_mb=8200.0,
            trip_kind="governor_kill",
            reroute_to="out_of_core",
            error=None,
        )
        trip_record = telemetry_record_from_governor_trip(
            trip, schema_fingerprint="fp-a", raw_bytes=1 * _GB, predicted_bytes=3 * _GB
        )
        records = [_record(raw_bytes=_GB, actual_peak_bytes=1 * _GB) for _ in range(30)]
        records.append(trip_record)
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.direction == "raise"
        # 8200 MB kill peak at a 1 GB basis, in-core intercept (200 MiB)
        # removed -> a 7.8125 per-byte slope: the trip still pushes k sharply
        # up, just measured against the pinned slope, not the point ratio.
        assert result.suggested_k == pytest.approx(7.8125)

    def test_trip_actual_peak_floors_at_budget_even_if_last_sample_was_lower(self) -> None:
        trip = GovernorTripRecord(
            route="full_frame",
            budget_bytes=8 * _GB,
            observed_peak_mb=7000.0,  # sampled slightly under budget (poll lag)
            trip_kind="self_oom",
            reroute_to="out_of_core",
            error=None,
        )
        record = telemetry_record_from_governor_trip(
            trip, schema_fingerprint="fp-a", raw_bytes=1 * _GB, predicted_bytes=3 * _GB
        )
        assert record.actual_peak_bytes == 8 * _GB

    def test_non_memory_trip_kind_is_rejected(self) -> None:
        trip = GovernorTripRecord(
            route="out_of_core",
            budget_bytes=8 * _GB,
            observed_peak_mb=None,
            trip_kind="route_ineligible",
            reroute_to="sequential",
            error="is not out-of-core-eligible",
        )
        with pytest.raises(ValueError, match="not a memory-miss kind"):
            telemetry_record_from_governor_trip(
                trip, schema_fingerprint="fp-a", raw_bytes=1 * _GB, predicted_bytes=1 * _GB
            )

    def test_crashed_trip_kind_is_rejected(self) -> None:
        trip = GovernorTripRecord(
            route="full_frame",
            budget_bytes=8 * _GB,
            observed_peak_mb=None,
            trip_kind="crashed",
            reroute_to=None,
            error="boom",
        )
        with pytest.raises(ValueError, match="not a memory-miss kind"):
            telemetry_record_from_governor_trip(
                trip, schema_fingerprint="fp-a", raw_bytes=1 * _GB, predicted_bytes=1 * _GB
            )


class TestPercentileMustBeMax:
    """HIGH remediation: `percentile < 1.0` has no safe semantics on the
    lowering side -- it must be rejected outright, not merely "allowed but
    risky". This is the dangerous path that previously had zero coverage:
    99 low samples + one real high outlier, recalibrated with a sub-max
    percentile, used to silently suggest a `k` 50x under the true peak.
    """

    def test_sub_max_percentile_is_rejected_even_with_a_high_outlier(self) -> None:
        records = [_record(raw_bytes=_GB, actual_peak_bytes=1 * _GB) for _ in range(99)]
        records.append(_record(raw_bytes=_GB, actual_peak_bytes=100 * _GB))
        with pytest.raises(ValueError, match="percentile"):
            recalibrate_k(records, "full_frame", current_k=3.0, percentile=0.99)

    def test_sub_max_percentile_rejected_even_when_it_would_only_raise(self) -> None:
        # Not just the "lowering" case -- the parameter is rejected
        # regardless of which direction the (never-computed) suggestion
        # would have gone, since the loop must not depend on the caller
        # picking the safe direction correctly.
        records = [_record(raw_bytes=_GB, actual_peak_bytes=10 * _GB)]
        with pytest.raises(ValueError, match="percentile"):
            recalibrate_k(records, "full_frame", current_k=3.0, percentile=0.5)

    def test_percentile_exactly_one_still_works(self) -> None:
        records = [_record(raw_bytes=_GB, actual_peak_bytes=10 * _GB)]
        result = recalibrate_k(records, "full_frame", current_k=3.0, percentile=1.0)
        assert result.direction == "raise"


class TestIsolatedOutcomeClassification:
    """MEDIUM remediation: a `crashed` (non-memory) isolated run must not be
    counted as evidence a schema fits at a low k -- it is excluded, mirroring
    `_MEMORY_MISS_TRIP_KINDS`'s governor-trip exclusion of the same failure
    class. `oom_killed` is a real memory observation and is floored at the
    mem cap (peak >= cap by construction), mirroring the governor trip's
    budget-floor.
    """

    def test_crashed_isolated_run_does_not_count_toward_recalibration(self) -> None:
        crashed_result = _isolated_result(peak_rss_mb=512.0, outcome="crashed", isolated=True)
        crashed_record = telemetry_record_from_isolated_run(
            crashed_result,
            schema_fingerprint="fp-a",
            path="full_frame",
            raw_bytes=1 * _GB,
            predicted_bytes=1 * _GB,
        )
        assert crashed_record.outcome == "crashed"

        low_k_records = [
            _record(raw_bytes=_GB, actual_peak_bytes=int(0.5 * _GB)) for _ in range(25)
        ]
        records = [*low_k_records, crashed_record]
        result = recalibrate_k(records, "full_frame", current_k=3.0, floor_k=0.1)
        # The crashed record must not have inflated the lowering sample count.
        assert result.sample_count == 25

    def test_oom_killed_isolated_run_is_floored_at_the_mem_cap(self) -> None:
        result_obj = _isolated_result(peak_rss_mb=4000.0, outcome="oom_killed", isolated=True)
        record = telemetry_record_from_isolated_run(
            result_obj,
            schema_fingerprint="fp-a",
            path="full_frame",
            raw_bytes=1 * _GB,
            predicted_bytes=3 * _GB,
            mem_cap_bytes=8 * _GB,
        )
        assert record.outcome == "self_oom"
        # 4000 MB was merely the last-sampled reading -- a self-OOM means
        # the true peak was AT LEAST the cap that triggered the kill.
        assert record.actual_peak_bytes == 8 * _GB

        result = recalibrate_k([record], "full_frame", current_k=3.0)
        assert result.direction == "raise"
        # 8 GB floored peak at a 1 GB basis, in-core intercept (200 MiB)
        # removed -> ~7.80 per-byte slope.
        assert result.suggested_k == pytest.approx(7.8046875)

    def test_oom_killed_without_a_mem_cap_uses_the_reported_peak(self) -> None:
        result_obj = _isolated_result(peak_rss_mb=8192.0, outcome="oom_killed", isolated=True)
        record = telemetry_record_from_isolated_run(
            result_obj,
            schema_fingerprint="fp-a",
            path="full_frame",
            raw_bytes=1 * _GB,
            predicted_bytes=3 * _GB,
        )
        assert record.actual_peak_bytes == int(8192.0 * _MB)


class TestMinSampleGateAndFloor:
    def test_too_few_records_never_lowers(self) -> None:
        # All observed k well below current_k, but only 3 samples -- far
        # under the default min_samples_for_lower (20).
        records = [_record(raw_bytes=_GB, actual_peak_bytes=int(0.5 * _GB)) for _ in range(3)]
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.direction == "hold"
        assert result.gates_passed is False
        assert result.suggested_k == 3.0

    def test_enough_samples_but_margin_not_cleared_never_lowers(self) -> None:
        # 25 samples (over the min), but k=2.9 is not far enough below 3.0
        # to clear the default 15% margin.
        records = [_record(raw_bytes=_GB, actual_peak_bytes=int(2.9 * _GB)) for _ in range(25)]
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.direction == "hold"
        assert result.gates_passed is False

    def test_enough_samples_and_margin_cleared_lowers(self) -> None:
        records = [
            _record(raw_bytes=_GB, actual_peak_bytes=_peak_for_slope(1.0, raw_bytes=_GB))
            for _ in range(25)
        ]
        result = recalibrate_k(records, "full_frame", current_k=3.0, floor_k=0.5)
        assert result.direction == "lower"
        assert result.gates_passed is True
        assert result.suggested_k == pytest.approx(1.0)

    def test_suggestion_never_crosses_the_floor(self) -> None:
        # Observed k is far below even the floor -- suggestion clamps AT
        # the floor, never below it, regardless of how low the telemetry
        # says the true ratio is.
        records = [_record(raw_bytes=_GB, actual_peak_bytes=int(0.05 * _GB)) for _ in range(50)]
        result = recalibrate_k(records, "full_frame", current_k=3.0, floor_k=1.5)
        assert result.direction == "lower"
        assert result.suggested_k == pytest.approx(1.5)
        assert result.suggested_k >= 1.5

    def test_default_floor_is_used_when_not_given(self) -> None:
        records = [_record(raw_bytes=_GB, actual_peak_bytes=int(0.01 * _GB)) for _ in range(50)]
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.suggested_k == pytest.approx(1.5)  # _K_FLOOR_DEFAULT["full_frame"]

    def test_floor_above_current_k_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="floor_k"):
            recalibrate_k([], "full_frame", current_k=1.0, floor_k=2.0)

    def test_a_lowering_can_never_undershoot_what_the_samples_support(self) -> None:
        # The one property that must hold no matter how the gates/floor are
        # tuned: suggested_k is never below the observed high-percentile
        # UNLESS the floor itself is higher (a safety override, not an
        # under-shoot). Sweep several floors and confirm the invariant.
        observed_high = 0.9
        records = [
            _record(raw_bytes=_GB, actual_peak_bytes=_peak_for_slope(observed_high, raw_bytes=_GB))
            for _ in range(50)
        ]
        for floor in (0.1, 0.5, 0.85, 0.95, 1.2):
            result = recalibrate_k(records, "full_frame", current_k=3.0, floor_k=min(floor, 3.0))
            assert result.suggested_k >= min(observed_high, result.floor_k)
            assert result.suggested_k >= result.floor_k


class TestMinSampleBoundary:
    """LOW-1: pin the `>=` boundary on `min_samples_for_lower` exactly, not
    just "well above" / "well below" it.
    """

    def test_exactly_min_samples_lowers_when_other_gates_pass(self) -> None:
        n = 20  # _DEFAULT_MIN_SAMPLES_FOR_LOWER
        records = [_record(raw_bytes=_GB, actual_peak_bytes=int(1.0 * _GB)) for _ in range(n)]
        result = recalibrate_k(records, "full_frame", current_k=3.0, floor_k=0.5)
        assert result.sample_count == n
        assert result.direction == "lower"
        assert result.gates_passed is True

    def test_one_below_min_samples_holds(self) -> None:
        n = 19
        records = [_record(raw_bytes=_GB, actual_peak_bytes=int(1.0 * _GB)) for _ in range(n)]
        result = recalibrate_k(records, "full_frame", current_k=3.0, floor_k=0.5)
        assert result.sample_count == n
        assert result.direction == "hold"
        assert result.gates_passed is False


class TestRaiseVsLowerAsymmetry:
    def test_raise_requires_no_minimum_sample_count(self) -> None:
        result = recalibrate_k(
            [_record(raw_bytes=_GB, actual_peak_bytes=10 * _GB)], "full_frame", current_k=3.0
        )
        assert result.sample_count == 1
        assert result.direction == "raise"
        assert result.gates_passed is True

    def test_equal_slope_holds_without_penalty(self) -> None:
        records = [
            _record(raw_bytes=_GB, actual_peak_bytes=_peak_for_slope(3.0, raw_bytes=_GB))
            for _ in range(50)
        ]
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.direction == "hold"
        assert result.gates_passed is True
        assert result.suggested_k == 3.0

    def test_no_records_at_all_holds_with_gates_failed(self) -> None:
        result = recalibrate_k([], "full_frame", current_k=3.0)
        assert result.sample_count == 0
        assert result.direction == "hold"
        assert result.gates_passed is False


class TestRecalibrationScopedBySchemaFingerprint:
    def test_schema_fingerprint_filter_restricts_the_pool(self) -> None:
        records = [
            _record(fingerprint="fp-a", raw_bytes=_GB, actual_peak_bytes=10 * _GB),
            _record(fingerprint="fp-b", raw_bytes=_GB, actual_peak_bytes=1 * _GB),
        ]
        # Scoped to fp-b only: the fp-a outlier (k=10) must not leak in, so
        # a single low (k=1) sample -- far under the min-samples-for-lower
        # gate -- can only hold, never raise off fp-a's evidence.
        result = recalibrate_k(
            records, "full_frame", current_k=3.0, schema_fingerprint="fp-b", floor_k=0.5
        )
        assert result.sample_count == 1
        assert result.direction == "hold"
        assert result.suggested_k == 3.0

    def test_unscoped_recalibration_sees_every_schema(self) -> None:
        records = [
            _record(
                fingerprint="fp-a",
                raw_bytes=_GB,
                actual_peak_bytes=_peak_for_slope(10.0, raw_bytes=_GB),
            ),
            _record(
                fingerprint="fp-b",
                raw_bytes=_GB,
                actual_peak_bytes=_peak_for_slope(1.0, raw_bytes=_GB),
            ),
        ]
        result = recalibrate_k(records, "full_frame", current_k=3.0)
        assert result.sample_count == 2
        assert result.direction == "raise"
        assert result.suggested_k == pytest.approx(10.0)


class TestRecalibrateValidation:
    def test_percentile_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="percentile"):
            recalibrate_k([], "full_frame", current_k=3.0, percentile=0.0)
        with pytest.raises(ValueError, match="percentile"):
            recalibrate_k([], "full_frame", current_k=3.0, percentile=1.5)

    def test_negative_lower_margin_rejected(self) -> None:
        with pytest.raises(ValueError, match="lower_margin"):
            recalibrate_k([], "full_frame", current_k=3.0, lower_margin=-0.1)

    def test_non_positive_min_samples_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_samples_for_lower"):
            recalibrate_k([], "full_frame", current_k=3.0, min_samples_for_lower=0)


# ---------------------------------------------------------------------------
# telemetry_record_from_isolated_run
# ---------------------------------------------------------------------------


def _isolated_result(
    *, peak_rss_mb: float | None, outcome: str, isolated: bool
) -> IsolatedRunResult:
    return IsolatedRunResult(
        outcome=outcome,  # type: ignore[arg-type]
        peak_rss_mb=peak_rss_mb,
        outputs={} if outcome == "completed" else None,
        quality_metrics={},
        table_kinds={},
        returncode=0,
        signal_number=None,
        error=None,
        isolated=isolated,
    )


class TestTelemetryRecordFromIsolatedRun:
    def test_completed_run_builds_a_completed_record(self) -> None:
        result = _isolated_result(peak_rss_mb=4096.0, outcome="completed", isolated=True)
        record = telemetry_record_from_isolated_run(
            result,
            schema_fingerprint="fp-a",
            path="full_frame",
            raw_bytes=1 * _GB,
            predicted_bytes=3 * _GB,
        )
        assert record.outcome == "completed"
        assert record.isolated is True
        assert record.actual_peak_bytes == int(4096.0 * _MB)

    def test_self_oom_with_a_peak_builds_a_self_oom_record(self) -> None:
        result = _isolated_result(peak_rss_mb=8192.0, outcome="oom_killed", isolated=True)
        record = telemetry_record_from_isolated_run(
            result,
            schema_fingerprint="fp-a",
            path="full_frame",
            raw_bytes=1 * _GB,
            predicted_bytes=3 * _GB,
        )
        assert record.outcome == "self_oom"

    def test_missing_peak_is_rejected(self) -> None:
        result = _isolated_result(peak_rss_mb=None, outcome="crashed", isolated=True)
        with pytest.raises(ValueError, match="peak_rss_mb"):
            telemetry_record_from_isolated_run(
                result,
                schema_fingerprint="fp-a",
                path="full_frame",
                raw_bytes=1 * _GB,
                predicted_bytes=1 * _GB,
            )

    def test_non_isolated_run_carries_isolated_false_through(self) -> None:
        result = _isolated_result(peak_rss_mb=4096.0, outcome="completed", isolated=False)
        record = telemetry_record_from_isolated_run(
            result,
            schema_fingerprint="fp-a",
            path="full_frame",
            raw_bytes=1 * _GB,
            predicted_bytes=3 * _GB,
        )
        assert record.isolated is False
        # And such a record is excluded by recalibrate_k -- end-to-end check
        # that the two modules compose correctly on the safety property.
        result2 = recalibrate_k([record], "full_frame", current_k=3.0)
        assert result2.sample_count == 0


# ---------------------------------------------------------------------------
# #74 basis contract: a sequential record must divide observed_slope by the
# WORKING-SET basis, not total raw bytes.
# ---------------------------------------------------------------------------


class TestSequentialBasisContract:
    """The estimator's sequential basis is the working set (two largest tables
    + FK dedup), NOT total raw bytes, so `raw_bytes` on a sequential telemetry
    record must be that working-set figure or `observed_slope` under-states the
    slope (OOM-unsafe). The `telemetry_record_from_*` builders enforce it."""

    @staticmethod
    def _three_tables() -> tuple[TableSizeSpec, ...]:
        # Three tables of DIFFERENT sizes so working-set (two largest) != total.
        return (
            _table("big", 1_000_000, (ColumnSizeSpec(name="a", dtype="int64"),)),
            _table("mid", 500_000, (ColumnSizeSpec(name="a", dtype="int64"),)),
            _table("small", 100_000, (ColumnSizeSpec(name="a", dtype="int64"),)),
        )

    def _seq_result(self) -> IsolatedRunResult:
        return _isolated_result(peak_rss_mb=1024.0, outcome="completed", isolated=True)

    def test_sequential_record_requires_tables(self) -> None:
        with pytest.raises(ValueError, match="basis contract"):
            telemetry_record_from_isolated_run(
                self._seq_result(),
                schema_fingerprint="fp",
                path="sequential",
                raw_bytes=12_000_000,
                predicted_bytes=1 * _GB,
            )

    def test_sequential_record_rejects_total_raw_bytes_basis(self) -> None:
        tables = self._three_tables()
        total_raw = sum(estimator_basis_bytes((t,), "full_frame").basis_bytes or 0 for t in tables)
        working_set = estimator_basis_bytes(tables, "sequential").basis_bytes
        assert working_set is not None and total_raw != working_set  # the footgun exists
        with pytest.raises(ValueError, match="basis contract"):
            telemetry_record_from_isolated_run(
                self._seq_result(),
                schema_fingerprint="fp",
                path="sequential",
                raw_bytes=total_raw,  # WRONG basis for sequential
                predicted_bytes=1 * _GB,
                tables=tables,
            )

    def test_sequential_record_accepts_working_set_basis(self) -> None:
        tables = self._three_tables()
        working_set = estimator_basis_bytes(tables, "sequential").basis_bytes
        assert working_set is not None
        record = telemetry_record_from_isolated_run(
            self._seq_result(),
            schema_fingerprint="fp",
            path="sequential",
            raw_bytes=working_set,  # correct working-set basis
            predicted_bytes=1 * _GB,
            tables=tables,
        )
        assert record.raw_bytes == working_set

    def test_in_core_record_without_tables_is_still_allowed(self) -> None:
        # Back-compat: full_frame/out_of_core records (basis == total raw bytes)
        # need not pass `tables`; only sequential is mandatory.
        record = telemetry_record_from_isolated_run(
            self._seq_result(),
            schema_fingerprint="fp",
            path="full_frame",
            raw_bytes=1 * _GB,
            predicted_bytes=3 * _GB,
        )
        assert record.path == "full_frame"


# ---------------------------------------------------------------------------
# MemoryTelemetryStore
# ---------------------------------------------------------------------------


class TestMemoryTelemetryStore:
    def test_add_and_all_round_trip(self) -> None:
        store = MemoryTelemetryStore()
        record = _record(actual_peak_bytes=3 * _GB)
        store.add(record)
        assert store.all() == (record,)

    def test_recalibrate_delegates_to_the_module_function(self) -> None:
        store = MemoryTelemetryStore()
        for _ in range(30):
            store.add(
                _record(raw_bytes=_GB, actual_peak_bytes=_peak_for_slope(10.0, raw_bytes=_GB))
            )
        result = store.recalibrate("full_frame", current_k=3.0)
        assert isinstance(result, KRecalibration)
        assert result.direction == "raise"
        assert result.suggested_k == pytest.approx(10.0)

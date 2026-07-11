"""Sprint B2: the two-point micro-probe (`_probe.py`). OOM-avoidance routing
redesign, docs/plans/2026-07-10-oom-avoidance-routing-redesign.md §3.3
(corrected per §11) -- the fast-path RECOVERY mechanism §13 names.

`run_pipeline_isolated` is MOCKED throughout (injected via `run_isolated=`)
so these tests pin the probe's MATH and GUARDS without paying for a real
subprocess; `test_isolated_run.py` already covers `run_pipeline_isolated`
itself, and this module's own docstring covers ONE real integration test
through the real primitive.

Four groups:

  - `TestTwoPointMethod`: the two-point fit recovers the true slope by
    canceling the fixed intercept -- and a naive single-point read (which
    conflates intercept into slope) over-predicts, the exact §11 defect
    this sprint fixes.
  - `TestGuards`: every inconclusive path -- OOM/crash/timeout, a
    non-isolated (contaminated) measurement, a missing peak, a degenerate
    or non-positive slope, a negative extrapolation, uniqueness-saturation
    risk, and opaque/nonlinear generators. Each MUST route bounded (never
    full_frame) via `probe_fits` returning `None`/`False`, never `True`.
  - `TestProbeMechanics`: `execution_mode` is always forced to `full_frame`
    (and rejecting a caller-supplied one), the reference-table lookup, and
    that a short-circuited guard never spawns a subprocess.
  - `TestProbeFits` / `TestUniquenessSaturationRisk`: the two small pure
    helper functions in isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution._isolated_common import IsolatedRunResult
from decoy_engine.execution._probe import (
    ProbePoint,
    ProbeResult,
    probe_fits,
    probe_peak_bytes,
    uniqueness_saturation_risk,
)

_MIB = 1024 * 1024
_GB = 1024 * _MIB


def _fake_result(
    *,
    peak_rss_mb: float | None,
    outcome: str = "completed",
    isolated: bool = True,
    error: str | None = None,
) -> IsolatedRunResult:
    return IsolatedRunResult(
        outcome=outcome,  # type: ignore[arg-type]
        peak_rss_mb=peak_rss_mb,
        outputs={} if outcome == "completed" else None,
        quality_metrics={},
        table_kinds={},
        returncode=0 if outcome == "completed" else 1,
        signal_number=None,
        error=error,
        isolated=isolated,
    )


class _QueueRunIsolated:
    """A fake `run_isolated` returning one canned result per call, in order.

    Records every call's kwargs for assertions (e.g. `execution_mode` was
    forced, `mem_cap_bytes` was forwarded).
    """

    def __init__(self, results: list[IsolatedRunResult]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, config: dict[str, Any], sources: dict[str, pa.Table] | None, **kwargs: Any
    ) -> IsolatedRunResult:
        self.calls.append({"config": config, "sources": sources, **kwargs})
        return self._results.pop(0)


def _never_called(config: Any = None, sources: Any = None, **kwargs: Any) -> IsolatedRunResult:
    raise AssertionError("run_isolated must not be called when a guard short-circuits")


def _resident_job(rows: int) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    config = {
        "version": 1,
        "tables": [{"name": "t", "columns": [{"name": "email", "strategy": "faker"}]}],
    }
    sources = {"t": pa.table({"id": list(range(rows))})}
    return config, sources


# ---------------------------------------------------------------------------
# The two-point method itself
# ---------------------------------------------------------------------------


class TestTwoPointMethod:
    def test_recovers_the_true_slope_and_intercept_noiselessly(self) -> None:
        """Two points from a KNOWN linear model (peak = slope*rows +
        intercept) must recover that exact slope/intercept -- the whole
        point of fitting a line through two points instead of one."""
        intercept = 300 * _MIB
        slope = 4_000.0  # bytes/row -- roughly the B1 sweep's ~4.1 GB/1M scale
        config, sources = _resident_job(1_000_000)
        low_rows, high_rows = 10_000, 20_000  # 1% / 2% of a 1,000,000-row target
        low_peak = intercept + slope * low_rows
        high_peak = intercept + slope * high_rows
        fake = _QueueRunIsolated(
            [
                _fake_result(peak_rss_mb=low_peak / _MIB),
                _fake_result(peak_rss_mb=high_peak / _MIB),
            ]
        )
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.01, 0.02),
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert result.conclusive
        assert result.slope_bytes_per_row == pytest.approx(slope, rel=1e-6)
        assert result.intercept_bytes == pytest.approx(intercept, rel=1e-6)
        expected = intercept + slope * 1_000_000
        assert result.estimated_peak_bytes == pytest.approx(expected, rel=1e-6)

    def test_single_point_would_overpredict_but_two_point_does_not(self) -> None:
        """The §11 defect this sprint fixes: a single 1%-scale probe read as
        `peak / rows` folds the fixed intercept into an inflated apparent
        per-row rate and over-predicts at the full target row count. The
        two-point fit must NOT reproduce that over-prediction."""
        intercept = 300 * _MIB
        true_slope = 4_000.0
        target_rows = 1_000_000
        low_rows, high_rows = 10_000, 20_000
        low_peak = intercept + true_slope * low_rows
        high_peak = intercept + true_slope * high_rows

        # What a NAIVE single-point read (using only the low-scale probe)
        # would infer and extrapolate:
        naive_single_point_rate = low_peak / low_rows
        naive_single_point_estimate = naive_single_point_rate * target_rows

        fake = _QueueRunIsolated(
            [
                _fake_result(peak_rss_mb=low_peak / _MIB),
                _fake_result(peak_rss_mb=high_peak / _MIB),
            ]
        )
        result = probe_peak_bytes(
            {"version": 1, "tables": []},
            {"t": pa.table({"id": list(range(target_rows))})},
            reference_table="t",
            target_rows=target_rows,
            probe_fractions=(0.01, 0.02),
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert result.conclusive
        true_estimate = intercept + true_slope * target_rows
        # The two-point estimate matches the TRUE model...
        assert result.estimated_peak_bytes == pytest.approx(true_estimate, rel=1e-6)
        # ...while the naive single-point read is strictly larger (an
        # over-prediction), because it folds the positive intercept into
        # its per-row rate.
        assert naive_single_point_estimate > result.estimated_peak_bytes

    def test_intercept_cancels_out_of_the_slope_calculation(self) -> None:
        """Changing ONLY the intercept must not move the fitted slope at
        all -- proof the two-point method truly cancels it, rather than
        happening to agree in one example."""
        target_rows = 500_000
        low_rows, high_rows = 5_000, 10_000
        slope = 2_500.0
        for intercept in (0.0, 50 * _MIB, 500 * _MIB):
            fake = _QueueRunIsolated(
                [
                    _fake_result(peak_rss_mb=(intercept + slope * low_rows) / _MIB),
                    _fake_result(peak_rss_mb=(intercept + slope * high_rows) / _MIB),
                ]
            )
            result = probe_peak_bytes(
                {"version": 1, "tables": []},
                {"t": pa.table({"id": list(range(target_rows))})},
                reference_table="t",
                target_rows=target_rows,
                probe_fractions=(0.01, 0.02),
                floor_rows=0,
                run_isolated=fake,
                engine_version="test",
            )
            assert result.slope_bytes_per_row == pytest.approx(slope, rel=1e-6)


# ---------------------------------------------------------------------------
# Guards: every inconclusive path must route bounded, never full_frame
# ---------------------------------------------------------------------------


class TestGuards:
    def _run(self, run_isolated: Any, **overrides: Any) -> ProbeResult:
        config, sources = _resident_job(overrides.pop("rows", 100_000))
        kwargs: dict[str, Any] = dict(
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.01, 0.02),
            floor_rows=0,
            run_isolated=run_isolated,
            engine_version="test",
        )
        kwargs.update(overrides)
        return probe_peak_bytes(config, sources, **kwargs)

    def test_oom_on_first_probe_run_is_inconclusive(self) -> None:
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=None, outcome="oom_killed")])
        result = self._run(fake)
        assert not result.conclusive
        assert probe_fits(result, 10 * _GB) is None

    def test_crashed_run_is_inconclusive(self) -> None:
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=None, outcome="crashed", error="boom")])
        result = self._run(fake)
        assert not result.conclusive
        assert "crashed" in result.reason

    def test_timeout_shaped_crash_is_inconclusive(self) -> None:
        fake = _QueueRunIsolated(
            [
                _fake_result(
                    peak_rss_mb=None,
                    outcome="crashed",
                    error="child exceeded timeout_s=1800.0s and was killed",
                )
            ]
        )
        result = self._run(fake)
        assert not result.conclusive

    def test_non_isolated_measurement_is_inconclusive(self) -> None:
        """A contaminated (in-process fallback) measurement must never be
        trusted, even if it reports `completed` with a plausible peak."""
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=500.0, isolated=False)])
        result = self._run(fake)
        assert not result.conclusive
        assert "isolated" in result.reason

    def test_missing_peak_on_a_completed_run_is_inconclusive(self) -> None:
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=None, outcome="completed")])
        result = self._run(fake)
        assert not result.conclusive

    def test_second_probe_run_failure_is_also_inconclusive(self) -> None:
        fake = _QueueRunIsolated(
            [
                _fake_result(peak_rss_mb=100.0),
                _fake_result(peak_rss_mb=None, outcome="oom_killed"),
            ]
        )
        result = self._run(fake)
        assert not result.conclusive
        assert len(fake.calls) == 2

    def test_non_positive_slope_is_inconclusive(self) -> None:
        # peak DECREASES as rows increase -- noise/nonlinearity, not a
        # trustworthy full_frame model.
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=200.0), _fake_result(peak_rss_mb=150.0)])
        result = self._run(fake)
        assert not result.conclusive
        assert "slope" in result.reason

    def test_degenerate_equal_rows_is_inconclusive(self) -> None:
        """Both fractions floor to the SAME achieved row count (a tiny
        table + a floor larger than either scale) -- no slope is
        measurable even though both runs succeed."""
        config, sources = _resident_job(50)
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=100.0), _fake_result(peak_rss_mb=110.0)])
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.01, 0.02),
            floor_rows=2_000,  # >> the 50-row table -> both floor to 50
            run_isolated=fake,
            engine_version="test",
        )
        assert not result.conclusive
        assert "degenerate" in result.reason

    def test_negative_extrapolation_is_inconclusive(self) -> None:
        """An adversarial (but internally consistent -- positive slope,
        non-degenerate) pair whose fitted line crosses zero before the
        target row count must not be trusted."""
        config, sources = _resident_job(10_000)
        fake = _QueueRunIsolated(
            [_fake_result(peak_rss_mb=10.0), _fake_result(peak_rss_mb=2_010.0)]
        )
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=500,  # far below where this line crosses zero (~995 rows)
            probe_fractions=(0.1, 0.2),  # -> achieved rows 1_000 / 2_000
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert not result.conclusive
        assert "negative" in result.reason

    def test_uniqueness_saturation_risk_short_circuits_without_any_probe_run(self) -> None:
        result = probe_peak_bytes(
            *_resident_job(1_000),
            reference_table="t",
            target_rows=1_000_000,
            uniqueness_risk_columns=[("t", "unique_id")],
            run_isolated=_never_called,
            engine_version="test",
        )
        assert not result.conclusive
        assert "uniqueness_saturation_risk" in result.reason
        assert probe_fits(result, 10 * _GB) is None

    def test_opaque_generator_short_circuits_without_any_probe_run(self) -> None:
        result = probe_peak_bytes(
            *_resident_job(1_000),
            reference_table="t",
            target_rows=1_000_000,
            opaque_generator_tables=["statistical_table"],
            run_isolated=_never_called,
            engine_version="test",
        )
        assert not result.conclusive
        assert "opaque" in result.reason.lower() or "nonlinear" in result.reason.lower()


# ---------------------------------------------------------------------------
# Mechanics: forced execution_mode, reference-table resolution
# ---------------------------------------------------------------------------


class TestProbeMechanics:
    def test_execution_mode_is_always_forced_to_full_frame(self) -> None:
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=100.0), _fake_result(peak_rss_mb=110.0)])
        probe_peak_bytes(
            *_resident_job(100_000),
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.01, 0.02),
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert len(fake.calls) == 2
        assert all(call["execution_mode"] == "full_frame" for call in fake.calls)

    def test_caller_supplied_execution_mode_is_rejected_up_front(self) -> None:
        with pytest.raises(ValueError, match="execution_mode"):
            probe_peak_bytes(
                *_resident_job(1_000),
                reference_table="t",
                target_rows=1_000,
                run_isolated=_never_called,
                execution_mode="sequential",
            )

    def test_unknown_reference_table_is_inconclusive_without_a_probe_run(self) -> None:
        result = probe_peak_bytes(
            *_resident_job(1_000),
            reference_table="does_not_exist",
            target_rows=1_000_000,
            run_isolated=_never_called,
            engine_version="test",
        )
        assert not result.conclusive
        assert "does_not_exist" in result.reason

    def test_rejects_non_increasing_probe_fractions(self) -> None:
        with pytest.raises(ValueError, match="probe_fractions"):
            probe_peak_bytes(
                *_resident_job(1_000),
                reference_table="t",
                target_rows=1_000,
                probe_fractions=(0.02, 0.01),
                run_isolated=_never_called,
            )

    def test_rejects_non_positive_target_rows(self) -> None:
        with pytest.raises(ValueError, match="target_rows"):
            probe_peak_bytes(
                *_resident_job(1_000),
                reference_table="t",
                target_rows=0,
                run_isolated=_never_called,
            )


# ---------------------------------------------------------------------------
# probe_fits: the None/True/False contract mirroring _mem_estimate.fits
# ---------------------------------------------------------------------------


class TestProbeFits:
    def _conclusive(self, estimated_peak_bytes: int) -> ProbeResult:
        return ProbeResult(
            conclusive=True,
            reason="measured",
            estimated_peak_bytes=estimated_peak_bytes,
            slope_bytes_per_row=1.0,
            intercept_bytes=0.0,
            low_point=ProbePoint(rows=10, peak_bytes=10),
            high_point=ProbePoint(rows=20, peak_bytes=20),
        )

    def test_generous_budget_fits(self) -> None:
        result = self._conclusive(1 * _GB)
        assert probe_fits(result, 10 * _GB) is True

    def test_tight_budget_does_not_fit(self) -> None:
        result = self._conclusive(9 * _GB)
        assert probe_fits(result, 10 * _GB) is False

    def test_margin_is_applied(self) -> None:
        # 8 GB * 1.3 = 10.4 GB > 10 GB budget -> does NOT fit despite the
        # raw estimate being under budget.
        result = self._conclusive(8 * _GB)
        assert probe_fits(result, 10 * _GB, error_band=0.30) is False

    def test_inconclusive_result_is_none_not_false(self) -> None:
        result = ProbeResult(conclusive=False, reason="inconclusive for testing")
        assert probe_fits(result, 10 * _GB) is None

    def test_rejects_invalid_budget_and_error_band(self) -> None:
        result = self._conclusive(1 * _GB)
        with pytest.raises(ValueError, match="budget_bytes"):
            probe_fits(result, 0)
        with pytest.raises(ValueError, match="error_band"):
            probe_fits(result, 1 * _GB, error_band=-0.1)


class TestProbeResultInvariants:
    def test_conclusive_requires_estimated_peak_bytes(self) -> None:
        with pytest.raises(ValueError, match="conclusive=True"):
            ProbeResult(conclusive=True, reason="measured")

    def test_inconclusive_must_not_carry_an_estimate(self) -> None:
        with pytest.raises(ValueError, match="conclusive=False"):
            ProbeResult(conclusive=False, reason="nope", estimated_peak_bytes=100)


# ---------------------------------------------------------------------------
# uniqueness_saturation_risk in isolation
# ---------------------------------------------------------------------------


class TestUniquenessSaturationRisk:
    def test_flags_a_column_near_its_full_scale_cardinality(self) -> None:
        risk = uniqueness_saturation_risk(
            row_counts_at_target={"t": 1_000_000},
            distinct_counts={("t", "email"): 950_000},
        )
        assert risk == (("t", "email"),)

    def test_does_not_flag_a_low_cardinality_column(self) -> None:
        risk = uniqueness_saturation_risk(
            row_counts_at_target={"t": 1_000_000},
            distinct_counts={("t", "status"): 5},
        )
        assert risk == ()

    def test_respects_a_custom_threshold(self) -> None:
        risk = uniqueness_saturation_risk(
            row_counts_at_target={"t": 1_000},
            distinct_counts={("t", "c"): 300},
            threshold=0.25,
        )
        assert risk == (("t", "c"),)

    def test_skips_a_table_missing_from_the_target_row_counts(self) -> None:
        risk = uniqueness_saturation_risk(
            row_counts_at_target={},
            distinct_counts={("missing", "c"): 100},
        )
        assert risk == ()


# ---------------------------------------------------------------------------
# ONE real integration test: a tiny real job through the REAL
# `run_pipeline_isolated` (no mock) -- proves the subprocess wiring
# (payload write, fresh-execve spawn, VmHWM read, commit-or-discard) works
# end-to-end for the probe specifically, not just for `run_pipeline_isolated`
# in isolation (already covered by `test_isolated_run.py`). Kept fast: a
# single mask table, two small probe scales.
# ---------------------------------------------------------------------------


def _real_probe_config(tmp_path: Path, rows: int) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    table = pa.table({"raw_id": pa.array([f"id-{i}" for i in range(rows)], type=pa.string())})
    src_path = tmp_path / "t.parquet"
    pq.write_table(table, src_path)
    raw_config = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "path": str(src_path), "format": "parquet"}},
        "targets": {
            "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "raw_id",
                        "strategy": "faker",
                        "provider": "person_email",
                        "deterministic": True,
                        "namespace": "ns",
                    }
                ],
            }
        ],
    }
    config = PipelineConfig.model_validate(raw_config).model_dump()
    return config, {"t": table}


class TestRealIntegration:
    def test_real_subprocess_probe_runs_end_to_end(self, tmp_path: Path) -> None:
        rows = 20_000
        config, sources = _real_probe_config(tmp_path, rows)
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=rows,
            probe_fractions=(0.05, 0.5),  # -> 1,000 / 10,000 real rows
            floor_rows=100,
            engine_version="probe-integration-test",
        )
        # Both real subprocess runs must have produced a clean measured
        # point regardless of whether the fit itself ends up conclusive at
        # this trivially small scale (real-machine noise is possible at
        # tens-of-thousands of rows) -- this is what proves the isolation
        # primitive actually ran twice and reported cleanly.
        assert result.low_point is not None
        assert result.high_point is not None
        assert result.low_point.rows == 1_000
        assert result.high_point.rows == 10_000
        assert result.low_point.peak_bytes > 0
        assert result.high_point.peak_bytes > 0
        if result.conclusive:
            assert result.estimated_peak_bytes is not None
            assert result.estimated_peak_bytes > 0
            assert result.slope_bytes_per_row is not None

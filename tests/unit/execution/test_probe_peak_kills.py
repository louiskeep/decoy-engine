"""Mutation kills for `execution/_probe.probe_peak_bytes` (TQ isolated-substrate
grade, branch `tq/isolated-substrate-grade`).

`probe_peak_bytes` grades in-process: `run_pipeline_isolated` is INJECTED via
`run_isolated=` and every test here passes a fast in-process fake that returns
controlled peak-RSS measurements, so the two-point fit math and every guard
verdict run deterministically in the parent (no real subprocess). Companion of
`test_probe.py`, which pins the happy-path math; this file targets the exact
machine fields the surviving mutants flip that `test_probe.py`'s looser
assertions (`not result.conclusive`, no `low_point`/`high_point` check, no
`mem_cap_bytes` forwarding, no exact boundary) left alive.

Each test asserts the EXACT field a mutation moves:
  - verdict `is False` (not merely falsy) -- kills `conclusive=False -> None`,
    which `not result.conclusive` cannot distinguish;
  - the populated `low_point` / `high_point` on every inconclusive branch --
    kills `low_point=low_point -> None` and the kwarg-drop variants;
  - the exact `>= vs >` / `<= vs <` boundary on each `if` guard;
  - the forwarded `mem_cap_bytes` call arg;
  - the populated `reason` on the conclusive path.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._isolated_common import IsolatedRunResult
from decoy_engine.execution._probe import (
    ProbePoint,
    ProbeResult,
    probe_peak_bytes,
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
    """Fake `run_isolated` returning one canned result per call, in order,
    recording every call's kwargs (for `mem_cap_bytes` / `execution_mode`
    forwarding assertions)."""

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


def _pk(mb: float) -> int:
    """The peak_bytes the module stores for a given peak_rss_mb:
    `int(peak_rss_mb * _MIB)` (mirrors `_run_one_probe`)."""
    return int(mb * _MIB)


# ---------------------------------------------------------------------------
# Input-validation boundaries (muts 10, 15, 16, 17, 18)
# ---------------------------------------------------------------------------


class TestValidationBoundaries:
    def test_target_rows_one_is_accepted_not_rejected(self) -> None:
        """mut 10: `target_rows <= 0` -> `<= 1`. target_rows=1 is a legal
        positive input (the guard rejects only <= 0); the mutant rejects 1."""
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=100.0), _fake_result(peak_rss_mb=110.0)])
        result = probe_peak_bytes(
            *_resident_job(100_000),
            reference_table="t",
            target_rows=1,
            probe_fractions=(0.01, 0.02),
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        # Orig returns a ProbeResult; the mutant raises ValueError on
        # target_rows=1 before ever running.
        assert isinstance(result, ProbeResult)

    def test_low_fraction_zero_is_rejected(self) -> None:
        """mut 15: `0.0 < low_frac` -> `0.0 <= low_frac`. A 0.0 low fraction
        is degenerate and must be rejected by probe_peak_bytes' own guard;
        the mutant admits it."""
        with pytest.raises(ValueError, match="probe_fractions"):
            probe_peak_bytes(
                *_resident_job(1_000),
                reference_table="t",
                target_rows=1_000,
                probe_fractions=(0.0, 0.02),
                run_isolated=_never_called,
            )

    def test_equal_fractions_are_rejected(self) -> None:
        """mut 16: `low_frac < high_frac` -> `<=`. Equal fractions carry no
        two-point spread and must be rejected up front; the mutant admits
        them."""
        with pytest.raises(ValueError, match="probe_fractions"):
            probe_peak_bytes(
                *_resident_job(1_000),
                reference_table="t",
                target_rows=1_000,
                probe_fractions=(0.05, 0.05),
                run_isolated=_never_called,
            )

    def test_high_fraction_exactly_one_is_accepted(self) -> None:
        """mut 17: `high_frac <= 1.0` -> `high_frac < 1.0`. A high fraction of
        exactly 1.0 (the full table) is legal; the mutant rejects it."""
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=100.0), _fake_result(peak_rss_mb=110.0)])
        result = probe_peak_bytes(
            *_resident_job(100_000),
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.5, 1.0),
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert isinstance(result, ProbeResult)

    def test_high_fraction_above_one_is_rejected(self) -> None:
        """mut 18: `high_frac <= 1.0` -> `high_frac <= 2.0`. A fraction above
        the whole table is nonsensical and must be rejected; the mutant
        widens the ceiling to admit it."""
        with pytest.raises(ValueError, match="probe_fractions"):
            probe_peak_bytes(
                *_resident_job(1_000),
                reference_table="t",
                target_rows=1_000,
                probe_fractions=(0.5, 1.5),
                run_isolated=_never_called,
            )


# ---------------------------------------------------------------------------
# Short-circuit guard verdicts must be `is False`, not `None` (muts 20, 31)
# ---------------------------------------------------------------------------


class TestShortCircuitVerdictIsFalse:
    def test_uniqueness_verdict_is_false_not_none(self) -> None:
        """mut 20: uniqueness-risk branch `conclusive=False` -> `None`. The
        verdict field must be exactly `False`; `None` is falsy so
        `not result.conclusive` cannot see the flip."""
        result = probe_peak_bytes(
            *_resident_job(1_000),
            reference_table="t",
            target_rows=1_000_000,
            uniqueness_risk_columns=[("t", "unique_id")],
            run_isolated=_never_called,
            engine_version="test",
        )
        assert result.conclusive is False

    def test_opaque_generator_verdict_is_false_not_none(self) -> None:
        """mut 31: opaque-generator branch `conclusive=False` -> `None`."""
        result = probe_peak_bytes(
            *_resident_job(1_000),
            reference_table="t",
            target_rows=1_000_000,
            opaque_generator_tables=["statistical_table"],
            run_isolated=_never_called,
            engine_version="test",
        )
        assert result.conclusive is False


# ---------------------------------------------------------------------------
# mem_cap_bytes is forwarded to every probe run (mut 48)
# ---------------------------------------------------------------------------


class TestMemCapForwarding:
    def test_mem_cap_bytes_is_forwarded_to_each_run(self) -> None:
        """mut 48: the loop's `mem_cap_bytes=mem_cap_bytes` -> `None`. The
        caller's cap must reach every isolated run; the mutant drops it."""
        cap = 7 * _GB
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=100.0), _fake_result(peak_rss_mb=110.0)])
        probe_peak_bytes(
            *_resident_job(100_000),
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.01, 0.02),
            floor_rows=0,
            run_isolated=fake,
            mem_cap_bytes=cap,
            engine_version="test",
        )
        assert len(fake.calls) == 2
        assert all(call["mem_cap_bytes"] == cap for call in fake.calls)


# ---------------------------------------------------------------------------
# Degenerate equal-rows branch: verdict + points (muts 63, 65, 66, 69, 70)
# ---------------------------------------------------------------------------


class TestDegenerateEqualRowsBranch:
    def test_verdict_is_false_and_points_are_populated(self) -> None:
        """Both fractions floor to the same achieved rows (a 50-row table
        under a 2000-row floor). mut 63: `conclusive=False` -> `None`; muts
        65/69: `low_point` blanked/dropped -> `None`; muts 66/70:
        `high_point` blanked/dropped -> `None`."""
        config, sources = _resident_job(50)
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=100.0), _fake_result(peak_rss_mb=110.0)])
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.01, 0.02),
            floor_rows=2_000,  # >> the 50-row table -> both scales floor to 50
            run_isolated=fake,
            engine_version="test",
        )
        assert result.conclusive is False  # mut 63
        assert result.low_point == ProbePoint(rows=50, peak_bytes=_pk(100.0))  # muts 65, 69
        assert result.high_point == ProbePoint(rows=50, peak_bytes=_pk(110.0))  # muts 66, 70


# ---------------------------------------------------------------------------
# Slope guard: boundary + verdict + points (muts 92, 93, 94, 96, 97, 100, 101)
# ---------------------------------------------------------------------------


class TestSlopeGuard:
    def test_zero_slope_is_inconclusive_with_points(self) -> None:
        """A flat pair (equal peaks, increasing rows) fits slope == 0.0.
        mut 92: `slope <= 0` -> `slope < 0` would ACCEPT a zero slope; the
        orig rejects it. mut 94: `conclusive=False` -> `None`; muts 96/100
        and 97/101: `low_point`/`high_point` blanked/dropped -> `None`."""
        config, sources = _resident_job(10_000)
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=200.0), _fake_result(peak_rss_mb=200.0)])
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.1, 0.2),  # -> achieved rows 1000 / 2000
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert result.conclusive is False  # muts 92, 94
        assert "slope" in result.reason
        assert result.low_point == ProbePoint(rows=1_000, peak_bytes=_pk(200.0))  # muts 96, 100
        assert result.high_point == ProbePoint(rows=2_000, peak_bytes=_pk(200.0))  # muts 97, 101

    def test_fractional_slope_below_one_is_conclusive(self) -> None:
        """mut 93: `slope <= 0` -> `slope <= 1`. A genuine but shallow slope
        of 0.5 bytes/row is a valid positive measurement; the mutant's
        widened floor would reject any slope in (0, 1]."""
        config, sources = _resident_job(1_000_000)
        low_bytes = _pk(200.0)
        high_bytes = low_bytes + 400_000  # over delta_rows 800_000 -> slope 0.5
        fake = _QueueRunIsolated(
            [
                _fake_result(peak_rss_mb=200.0),
                _fake_result(peak_rss_mb=high_bytes / _MIB),
            ]
        )
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.1, 0.9),  # -> achieved rows 100_000 / 900_000
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert result.conclusive is True
        assert result.slope_bytes_per_row == pytest.approx(0.5, rel=1e-9)


# ---------------------------------------------------------------------------
# Negative-extrapolation guard: boundary + verdict + points
# (muts 108, 109, 110, 112, 113, 116, 117)
# ---------------------------------------------------------------------------


class TestNegativeExtrapolationGuard:
    def test_negative_extrapolation_verdict_and_points(self) -> None:
        """A positive-slope, non-degenerate pair whose fitted line has
        already crossed zero before the (small) target row count.
        mut 110: `conclusive=False` -> `None`; muts 112/116 and 113/117:
        `low_point`/`high_point` blanked/dropped -> `None`."""
        config, sources = _resident_job(10_000)
        fake = _QueueRunIsolated(
            [_fake_result(peak_rss_mb=10.0), _fake_result(peak_rss_mb=2_010.0)]
        )
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=500,  # well below where this line crosses zero (~995 rows)
            probe_fractions=(0.1, 0.2),  # -> achieved rows 1000 / 2000
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert result.conclusive is False  # mut 110
        assert "negative" in result.reason
        assert result.low_point == ProbePoint(rows=1_000, peak_bytes=_pk(10.0))  # muts 112, 116
        assert result.high_point == ProbePoint(rows=2_000, peak_bytes=_pk(2_010.0))  # muts 113, 117

    def test_estimated_exactly_zero_is_conclusive(self) -> None:
        """muts 108/109: `estimated < 0` -> `<= 0` / `< 1`. The same fitted
        line (slope 2*MiB/row, intercept -1990 MiB) extrapolated to exactly
        995 rows lands on estimated == 0 -- a legal non-negative estimate the
        orig accepts, but which BOTH mutants (`<= 0` and `< 1`) reject."""
        config, sources = _resident_job(10_000)
        fake = _QueueRunIsolated(
            [_fake_result(peak_rss_mb=10.0), _fake_result(peak_rss_mb=2_010.0)]
        )
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=995,  # slope*995 + intercept == 0 exactly
            probe_fractions=(0.1, 0.2),  # -> achieved rows 1000 / 2000
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert result.conclusive is True
        assert result.estimated_peak_bytes == 0


# ---------------------------------------------------------------------------
# raw_bytes floor guard: boundary + verdict + points
# (muts 121, 122, 124, 125, 128, 129)
# ---------------------------------------------------------------------------


class TestRawFloorGuard:
    def test_below_floor_verdict_and_points(self) -> None:
        """A near-flat (noise-driven) slope extrapolates far below the
        physical raw-bytes floor. mut 122: `conclusive=False` -> `None`;
        muts 124/128 and 125/129: `low_point`/`high_point` blanked/dropped
        -> `None`."""
        config, sources = _resident_job(1_000_000)
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=210.0), _fake_result(peak_rss_mb=220.0)])
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.01, 0.02),  # -> achieved rows 10_000 / 20_000
            floor_rows=0,
            run_isolated=fake,
            raw_floor_bytes=50 * _GB,  # far above the ~1.26 GB this slope lands on
            engine_version="test",
        )
        assert result.conclusive is False  # mut 122
        assert "floor" in result.reason.lower()
        assert result.low_point == ProbePoint(rows=10_000, peak_bytes=_pk(210.0))  # muts 124, 128
        assert result.high_point == ProbePoint(rows=20_000, peak_bytes=_pk(220.0))  # muts 125, 129

    def test_estimate_exactly_at_floor_is_conclusive(self) -> None:
        """mut 121: `estimated < raw_floor_bytes` -> `estimated <=
        raw_floor_bytes`. An estimate landing EXACTLY on the floor is not
        below it -- peak RSS == resident-input bytes is physically fine, so
        the orig accepts it; the mutant's `<=` rejects the boundary."""
        config, sources = _resident_job(1_000_000)
        fake = _QueueRunIsolated(
            [_fake_result(peak_rss_mb=200.0), _fake_result(peak_rss_mb=1_000.0)]
        )
        # slope = (1000-200)MiB / 80_000 rows; intercept = 0; at 1e6 rows the
        # estimate is exactly 10_485_760_000 B (verified integer-valued).
        result = probe_peak_bytes(
            config,
            sources,
            reference_table="t",
            target_rows=1_000_000,
            probe_fractions=(0.02, 0.10),  # -> achieved rows 20_000 / 100_000
            floor_rows=0,
            run_isolated=fake,
            raw_floor_bytes=10_485_760_000,
            engine_version="test",
        )
        assert result.conclusive is True
        assert result.estimated_peak_bytes == 10_485_760_000


# ---------------------------------------------------------------------------
# Conclusive path: reason stays populated (mut 137)
# ---------------------------------------------------------------------------


class TestConclusiveReasonPopulated:
    def test_conclusive_result_reason_is_not_none(self) -> None:
        """mut 137: the conclusive-path `reason="measured"` -> `None`. The
        docstring promises `reason` is ALWAYS populated; a `None` reason
        on a conclusive result breaks that invariant. (The prose-relabel
        variants of the same literal are accepted non-contract -- this
        asserts populated-ness, not the exact wording.)"""
        intercept = 300 * _MIB
        slope = 4_000.0
        target_rows = 1_000_000
        low_rows, high_rows = 20_000, 100_000  # the (0.02, 0.10) pair
        fake = _QueueRunIsolated(
            [
                _fake_result(peak_rss_mb=(intercept + slope * low_rows) / _MIB),
                _fake_result(peak_rss_mb=(intercept + slope * high_rows) / _MIB),
            ]
        )
        result = probe_peak_bytes(
            *_resident_job(target_rows),
            reference_table="t",
            target_rows=target_rows,
            probe_fractions=(0.02, 0.10),
            floor_rows=0,
            run_isolated=fake,
            engine_version="test",
        )
        assert result.conclusive is True
        assert result.reason is not None

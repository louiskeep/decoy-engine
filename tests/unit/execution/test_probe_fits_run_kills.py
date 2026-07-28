"""Mutation-kill tests for the FITS + RUN + small-helper functions of
`execution/_probe.py` (`probe_fits`, `_run_one_probe`, `_fit_line`,
`uniqueness_saturation_risk`).

Companion to `test_probe.py`: `run_pipeline_isolated` is injected via
`run_isolated=` so the probe logic runs in-process. Each test below pins the
EXACT machine field a surviving mutant flips (a fits/no-fits verdict at the
margin boundary, the default error-band value, a fitted line's slope/intercept,
a saturation-risk boundary, a `conclusive` verdict, or a forwarded run_isolated
argument), not message prose.

Accepted non-contract (message/telemetry prose, deliberately NOT pinned here):
`_run_one_probe` 18/19/20/21 (the no-scaled-row-count reason text) and 52 (the
empty-error suffix). Proven equivalent (unreachable defensive branch):
`probe_fits` 11-17 -- the `estimated_bytes is None` AssertionError is
unreachable because `ProbeResult.__post_init__` forbids conclusive=True with a
None estimate, so mutating that branch's message is unobservable.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.execution._isolated_common import IsolatedRunResult
from decoy_engine.execution._probe import (
    ProbePoint,
    ProbeResult,
    _fit_line,
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
    """Fake `run_isolated` returning one canned result per call, recording the
    kwargs of every call for forwarding assertions."""

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


def _conclusive(estimated_peak_bytes: int) -> ProbeResult:
    return ProbeResult(
        conclusive=True,
        reason="measured",
        estimated_peak_bytes=estimated_peak_bytes,
        slope_bytes_per_row=1.0,
        intercept_bytes=0.0,
        low_point=ProbePoint(rows=10, peak_bytes=10),
        high_point=ProbePoint(rows=20, peak_bytes=20),
    )


# ---------------------------------------------------------------------------
# probe_fits -- LOGIC survivors 1, 2, 6, 20, 21
# ---------------------------------------------------------------------------


class TestProbeFitsKills:
    def test_default_error_band_is_030_not_130(self) -> None:
        """mut_1: the default `error_band` is 0.30, not 1.3. A 1 GB estimate
        must FIT a 2 GB budget under the default margin (1.0*1.3=1.3 GB < 2
        GB); a 1.3 default would inflate it to 2.3 GB and report no-fit."""
        assert probe_fits(_conclusive(1 * _GB), 2 * _GB) is True

    def test_error_band_zero_is_allowed_not_rejected(self) -> None:
        """mut_2: the guard is `error_band < 0`, not `<= 0`. A zero band is a
        valid (no-margin) request and must not raise."""
        assert probe_fits(_conclusive(1 * _GB), 2 * _GB, error_band=0.0) is True

    def test_budget_of_one_byte_is_valid_not_rejected(self) -> None:
        """mut_6: the guard is `budget_bytes <= 0`, not `<= 1`. A 1-byte
        budget is the smallest positive budget and must not raise; a 0-byte
        estimate clears it."""
        assert probe_fits(_conclusive(0), 1) is True

    def test_margin_is_one_plus_error_band_not_two_plus(self) -> None:
        """mut_20: the multiplier is `(1 + error_band)`, not `(2 + ...)`. A 1
        GB estimate with a 0.30 band is 1.3 GB and FITS a 2 GB budget; the
        `2 + band` form would compute 2.3 GB and wrongly report no-fit."""
        assert probe_fits(_conclusive(1 * _GB), 2 * _GB, error_band=0.30) is True

    def test_comparison_is_strict_less_than_not_le(self) -> None:
        """mut_21: the fit test is `< budget`, not `<= budget`. An estimate
        that lands EXACTLY on the budget after margin does NOT fit."""
        # 1000 bytes * (1 + 0.0) == 1000 == budget -> strict `<` is False.
        assert probe_fits(_conclusive(1000), 1000, error_band=0.0) is False


# ---------------------------------------------------------------------------
# _run_one_probe -- LOGIC survivors 13, 24, 25, 31, 32, 46
# ---------------------------------------------------------------------------


def _probe(run_isolated: Any, *, reference_table: str = "t", **overrides: Any) -> ProbeResult:
    config, sources = _resident_job(overrides.pop("rows", 100_000))
    kwargs: dict[str, Any] = dict(
        reference_table=reference_table,
        target_rows=1_000_000,
        probe_fractions=(0.01, 0.02),
        floor_rows=0,
        run_isolated=run_isolated,
    )
    kwargs.update(overrides)
    return probe_peak_bytes(config, sources, **kwargs)


class TestRunOneProbeKills:
    def test_unknown_reference_table_verdict_is_exactly_false(self) -> None:
        """mut_13: the no-scaled-row-count branch sets `conclusive=False`, not
        `conclusive=None`. The verdict field must be the bool False."""
        result = _probe(_never_called, reference_table="does_not_exist")
        assert result.conclusive is False

    def test_failed_run_verdict_is_exactly_false(self) -> None:
        """mut_46: the unclean-measurement branch sets `conclusive=False`, not
        `conclusive=None`."""
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=None, outcome="oom_killed")])
        result = _probe(fake)
        assert result.conclusive is False

    def test_sources_are_forwarded_to_run_isolated(self) -> None:
        """mut_24: the real (downscaled) sources are passed to run_isolated,
        not `None`."""
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=100.0), _fake_result(peak_rss_mb=110.0)])
        _probe(fake)
        assert fake.calls[0]["sources"] is not None
        assert "t" in fake.calls[0]["sources"]

    def test_mem_cap_bytes_is_forwarded_verbatim(self) -> None:
        """mut_25 (hardcoded None) and mut_31 (kwarg dropped): the caller's
        `mem_cap_bytes` reaches run_isolated as itself and is present."""
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=100.0), _fake_result(peak_rss_mb=110.0)])
        _probe(fake, mem_cap_bytes=123_456_789)
        assert fake.calls[0]["mem_cap_bytes"] == 123_456_789

    def test_isolate_true_is_forwarded(self) -> None:
        """mut_32: `isolate=True` is always passed to run_isolated (dropping
        the kwarg would let the primitive's own default decide)."""
        fake = _QueueRunIsolated([_fake_result(peak_rss_mb=100.0), _fake_result(peak_rss_mb=110.0)])
        _probe(fake)
        assert fake.calls[0]["isolate"] is True


# ---------------------------------------------------------------------------
# _fit_line -- LOGIC survivors 3, 4 (tested directly: pure math helper)
# ---------------------------------------------------------------------------


class TestFitLineKills:
    def test_equal_rows_is_degenerate_returns_none(self) -> None:
        """mut_3: the degeneracy guard is `delta_rows <= 0`, not `< 0`. Equal
        rows (delta == 0) must return None, not fall through to a
        divide-by-zero."""
        assert (
            _fit_line(ProbePoint(rows=5, peak_bytes=100), ProbePoint(rows=5, peak_bytes=200))
            is None
        )

    def test_rows_differing_by_one_are_fitted_not_rejected(self) -> None:
        """mut_4: the guard is `delta_rows <= 0`, not `<= 1`. A one-row spread
        is a valid (if noisy) fit and must yield the exact line, not None."""
        fit = _fit_line(ProbePoint(rows=5, peak_bytes=100), ProbePoint(rows=6, peak_bytes=200))
        assert fit is not None
        slope, intercept = fit
        assert slope == 100.0
        assert intercept == 100.0 - 100.0 * 5  # -400.0


# ---------------------------------------------------------------------------
# uniqueness_saturation_risk -- LOGIC survivors 5, 7
# ---------------------------------------------------------------------------


class TestUniquenessSaturationRiskKills:
    def test_missing_target_row_count_is_skipped_not_break(self) -> None:
        """mut_5: a table missing from the row-count map is `continue`d over,
        not `break`ed on. A later risky column after a skipped one must still
        be flagged."""
        risk = uniqueness_saturation_risk(
            row_counts_at_target={"b": 1_000},  # "a" is intentionally absent
            distinct_counts={("a", "c1"): 100, ("b", "c2"): 900},
        )
        assert risk == (("b", "c2"),)

    def test_boundary_is_inclusive_ge_not_strict_gt(self) -> None:
        """mut_7: the threshold test is `>= threshold`, not `> threshold`. A
        column exactly AT the threshold is flagged."""
        risk = uniqueness_saturation_risk(
            row_counts_at_target={"t": 1_000},
            distinct_counts={("t", "c"): 500},  # 500/1000 == 0.5 == default threshold
        )
        assert risk == (("t", "c"),)

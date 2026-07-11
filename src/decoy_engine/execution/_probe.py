"""Sprint B2: the two-point micro-probe (OOM-avoidance routing redesign,
`docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §3.3, corrected per
§11; framed as the fast-path RECOVERY mechanism by §13).

`_mem_estimate.py`'s static estimator (B1a) is a deliberately CONSERVATIVE
filter -- `K_FULL_FRAME_COLD_START` is picked to never under-predict, which
means it over-prices a pooled-string schema by up to ~26x
(`K_FULL_FRAME_COLD_START / K_FULL_FRAME_MEASURED_POOLED` = 3.0 / 0.117-ish
true ratio) and routes it bounded even though it would have fit full_frame
comfortably. This module is how that fast path gets RECOVERED: instead of
trusting a model of bytes, it MEASURES the real peak RSS at a small scale
via `run_pipeline_isolated` (Sprint 1a's fresh-subprocess isolation
primitive) and extrapolates to the job's real (target) row count.

Two points, not one (§11's correctness fix): a single small-N probe reads
`peak = slope * rows + intercept`, but `intercept` (fixed interpreter /
import / Faker / DuckDB init cost -- unrelated to row count) is invisible
to a one-point read, which silently folds it into an inflated apparent
slope. The B1 measurement is direct evidence: a ~0.3-0.4 GB fixed intercept
against a ~4.1 GB/1M-rows true slope means a single 100k-row probe would
read ~7.1 GB/M (0.3 GB / 0.1 + 4.08 approx) against the true 4.08 GB/M --
roughly a 70% overestimate that would evict most mid-size jobs from the
very fast path this module exists to recover. Two points let the fixed
intercept cancel out of the slope calculation (`_fit_line` below), which is
the whole reason this module runs the job TWICE instead of once.

Every branch that cannot trust its own measurement returns
`ProbeResult(conclusive=False, ...)` -- inconclusive routes bounded, exactly
like `_mem_estimate.PeakEstimate.unpriceable` (§3.5's "never guess" rule
applied to a probe instead of a static estimate). `probe_fits` mirrors
`_mem_estimate.fits`'s `None`-vs-`False` contract for the same reason: "the
probe could not measure this" must never be silently coerced into "does not
fit" or, far worse, "fits" -- a caller (the router) must handle the
inconclusive case explicitly.

Scope limits inherited from Sprint 1a (`run_pipeline_isolated`'s own
docstring): `sink`/`source_loader`/`vault_writer`/`registry`/`derive_key`
must stay at their defaults for a probe run to cross the process boundary.
This does not weaken the measurement -- peak RSS is governed by row/column
SHAPE (dtype mix, string width, table count), not by which values a
provider derives or which key encrypts them -- but it does mean a probe run
always uses the default provider registry and no vault/custom sink,
regardless of what the real job requests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.execution._isolated_common import IsolatedRunResult
from decoy_engine.execution._isolated_run import run_pipeline_isolated
from decoy_engine.execution._probe_scale import (
    DEFAULT_PROBE_FLOOR_ROWS,
    downscale_job,
)

__all__ = [
    "DEFAULT_PROBE_FRACTIONS",
    "MIN_PLAUSIBLE_K_FULL_FRAME",
    "UNIQUENESS_SATURATION_THRESHOLD",
    "ProbePoint",
    "ProbeResult",
    "probe_fits",
    "probe_peak_bytes",
    "uniqueness_saturation_risk",
]

_MIB = 1024 * 1024

# ~1% / ~2% of the target row count (§3.3, corrected per §11 to be a PAIR,
# not a single point). Both fractions are cheap relative to a full run (the
# design doc's "~1-2% of a full run" framing), and the ratio between them
# (2x) is generous enough that floor-rounding noise at the low end does not
# dominate the measured slope.
DEFAULT_PROBE_FRACTIONS: tuple[float, float] = (0.01, 0.02)

# A column whose distinct-count is at/above this fraction of its FULL-SCALE
# (target) row count is flagged as an uniqueness-saturation risk (§11): pool
# exhaustion, dictionary-encoding fallback, and hash-table growth effects
# all happen near a column's true cardinality ceiling, which a 1-2% probe
# never approaches, so the probe's measured slope cannot see them. 0.5 is a
# conservative, round starting point -- a coarse filter, not a fitted
# constant; tightening it is future work once B5 telemetry exists.
UNIQUENESS_SATURATION_THRESHOLD = 0.5

# The lowest real full_frame peak/raw-bytes ratio this codebase has
# measured evidence for: the B1a calibration finding
# (`_mem_estimate.K_FULL_FRAME_COLD_START`'s docstring) measured a
# low-cardinality pooled-string schema's TRUE peak/raw at ~0.117 -- the most
# favorable real schema shape on record. Used ONLY as a "could ANY
# realistic schema possibly fit" pre-filter (skip a probe that cannot
# possibly help; see `_pipeline_routing_signals.resolve_probe_recovery`),
# never as a routing multiplier in its own right -- that is
# `_mem_estimate.K_FULL_FRAME_COLD_START`'s job, and it deliberately points
# the OTHER direction (conservative-high, not conservative-low).
MIN_PLAUSIBLE_K_FULL_FRAME = 0.117


@dataclass(frozen=True)
class ProbePoint:
    """One measured `(rows, peak_bytes)` sample from a single probe run."""

    rows: int
    peak_bytes: int

    def __post_init__(self) -> None:
        if self.rows < 0:
            raise ValueError(f"rows must be >= 0, got {self.rows}.")
        if self.peak_bytes < 0:
            raise ValueError(f"peak_bytes must be >= 0, got {self.peak_bytes}.")


@dataclass(frozen=True)
class ProbeResult:
    """The two-point probe's verdict.

    `conclusive=False` -- an OOM/crash/timeout on either probe run, a
    contaminated (non-isolated) measurement, a degenerate or non-positive
    fitted slope, a uniqueness-saturation risk, or an opaque/nonlinear
    generator -- MUST route bounded, exactly like `_mem_estimate.
    PeakEstimate.unpriceable` (§3.5's rule, applied here to a MEASUREMENT
    instead of a static model). `reason` is a human-readable diagnostic,
    always populated. The numeric fields are populated if and only if
    `conclusive` is True (enforced below): an inconclusive result never
    carries a number a caller could mistakenly trust.
    """

    conclusive: bool
    reason: str
    estimated_peak_bytes: int | None = None
    slope_bytes_per_row: float | None = None
    intercept_bytes: float | None = None
    low_point: ProbePoint | None = None
    high_point: ProbePoint | None = None

    def __post_init__(self) -> None:
        if self.conclusive and self.estimated_peak_bytes is None:
            raise ValueError("ProbeResult: conclusive=True requires estimated_peak_bytes.")
        if not self.conclusive and self.estimated_peak_bytes is not None:
            raise ValueError("ProbeResult: conclusive=False must not carry estimated_peak_bytes.")


def uniqueness_saturation_risk(
    row_counts_at_target: Mapping[str, int],
    distinct_counts: Mapping[tuple[str, str], int],
    *,
    threshold: float = UNIQUENESS_SATURATION_THRESHOLD,
) -> tuple[tuple[str, str], ...]:
    """`(table, column)` pairs whose FULL-SCALE distinct-count is at/above
    `threshold` of that table's FULL-SCALE (target) row count -- the §11
    uniqueness-saturation blind spot a small-N probe cannot see.

    `distinct_counts` is keyed `(table_name, column_name)` -- the same shape
    as `_mem_estimate.UnpriceableColumn` -- typically sourced from
    `ColumnProfile.distinct_count` at the job's REAL scale (not the probe's
    downscaled one; the whole point is comparing against the FULL-SCALE
    row count, which is what `row_counts_at_target` must carry). A missing
    or falsy target row count for a table is skipped rather than treated as
    a division error -- the caller's row-count map is expected to cover
    every table it also supplies distinct-counts for, but a defensive
    caller passing a partial map should not crash this pure function.
    """
    risky: list[tuple[str, str]] = []
    for (table, column), distinct in distinct_counts.items():
        target_rows = row_counts_at_target.get(table)
        if not target_rows:
            continue
        if distinct / target_rows >= threshold:
            risky.append((table, column))
    return tuple(risky)


def _fit_line(low: ProbePoint, high: ProbePoint) -> tuple[float, float] | None:
    """`(slope, intercept)` from two points, or `None` if degenerate
    (non-increasing rows -- the caller must supply an increasing pair)."""
    delta_rows = high.rows - low.rows
    if delta_rows <= 0:
        return None
    slope = (high.peak_bytes - low.peak_bytes) / delta_rows
    intercept = low.peak_bytes - slope * low.rows
    return slope, intercept


def _run_one_probe(
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None,
    *,
    reference_table: str,
    fraction: float,
    floor_rows: int,
    run_isolated: Callable[..., IsolatedRunResult],
    mem_cap_bytes: int | None,
    timeout_s: float | None,
    run_pipeline_kwargs: dict[str, Any],
) -> ProbePoint | ProbeResult:
    """One probe run at `fraction` of the job's rows. Returns a `ProbePoint`
    on a clean measured peak, or a `ProbeResult(conclusive=False, ...)`
    naming why the run cannot be trusted -- the caller propagates the
    latter straight through as the whole probe's verdict.
    """
    scaled = downscale_job(config, sources, fraction, floor_rows=floor_rows)
    achieved_rows = scaled.row_counts.get(reference_table)
    if achieved_rows is None:
        return ProbeResult(
            conclusive=False,
            reason=(
                f"reference_table {reference_table!r} has no scaled row count "
                "(not a resident source or a generate table) -- cannot anchor "
                "the probe's x-axis"
            ),
        )
    # Forced full_frame (§13's whole point: measure the FULL_FRAME peak
    # specifically, not whatever the downscaled job's own auto-router would
    # pick -- a small relationship-bearing pure-mask job would otherwise
    # legacy-route to `sequential` under the row-count gate and measure the
    # wrong path entirely).
    result = run_isolated(
        scaled.config,
        scaled.sources,
        mem_cap_bytes=mem_cap_bytes,
        isolate=True,
        timeout_s=timeout_s,
        execution_mode="full_frame",
        **run_pipeline_kwargs,
    )
    if result.outcome != "completed" or not result.isolated or result.peak_rss_mb is None:
        return ProbeResult(
            conclusive=False,
            reason=(
                f"probe run at {fraction:.2%} ({achieved_rows:,} rows) did not "
                f"yield a clean measured peak: outcome={result.outcome!r}, "
                f"isolated={result.isolated!r}, peak_rss_mb={result.peak_rss_mb!r}"
                + (f"; error={result.error}" if result.error else "")
            ),
        )
    return ProbePoint(rows=achieved_rows, peak_bytes=int(result.peak_rss_mb * _MIB))


def probe_peak_bytes(
    config: dict[str, Any],
    sources: dict[str, pa.Table] | None,
    *,
    reference_table: str,
    target_rows: int,
    probe_fractions: tuple[float, float] = DEFAULT_PROBE_FRACTIONS,
    floor_rows: int = DEFAULT_PROBE_FLOOR_ROWS,
    uniqueness_risk_columns: Sequence[tuple[str, str]] = (),
    opaque_generator_tables: Sequence[str] = (),
    run_isolated: Callable[..., IsolatedRunResult] = run_pipeline_isolated,
    mem_cap_bytes: int | None = None,
    timeout_s: float | None = None,
    **run_pipeline_kwargs: Any,
) -> ProbeResult:
    """Measure `config`'s real full_frame peak RSS at two small scales of
    `reference_table` and extrapolate to `target_rows` (§3.3/§11's two-point
    method). `reference_table` must be a key in `sources` (resident) or a
    generate-kind table name in `config` -- it anchors both probe points'
    x-axis and the extrapolation target.

    Every run this function makes goes through `run_isolated` (by default
    `run_pipeline_isolated`) with `isolate=True` and `execution_mode=
    "full_frame"` forced -- passing `execution_mode` in `run_pipeline_kwargs`
    is rejected up front rather than silently overridden, since a caller
    doing that almost certainly does not understand what this function
    measures.

    Guards, evaluated in order (each short-circuits to `conclusive=False`,
    never runs a subprocess it does not need to):

      1. `uniqueness_risk_columns` non-empty -- §11's blind spot; a small-N
         probe cannot see saturation near a column's true cardinality.
      2. `opaque_generator_tables` non-empty -- a nonlinear/data-dependent
         generator (e.g. `statistical`, `derived_aggregate`) whose behavior
         at small N is not representative of its behavior at full scale.
      3. Either probe run OOMs/crashes/times out, or is not a clean
         isolated measurement.
      4. The two achieved row counts are equal (a degenerate pair, usually
         both floored to the same value) or otherwise non-increasing.
      5. The fitted slope is <= 0 (noise, or a genuinely flat/nonlinear
         relationship the model does not support).
      6. The extrapolated estimate is negative (an untrustworthy fit).

    `estimated_peak_bytes` is the raw extrapolation with NO margin applied
    -- `probe_fits` is where the asymmetric error_band margin (mirroring
    `_mem_estimate.fits`) gets applied for a routing decision.
    """
    if "execution_mode" in run_pipeline_kwargs:
        raise ValueError(
            "probe_peak_bytes always measures the full_frame path; do not pass "
            "execution_mode in run_pipeline_kwargs (it is set internally)."
        )
    if target_rows <= 0:
        raise ValueError(f"target_rows must be positive, got {target_rows}.")
    low_frac, high_frac = probe_fractions
    if not 0.0 < low_frac < high_frac <= 1.0:
        raise ValueError(
            f"probe_fractions must be an increasing pair in (0, 1], got {probe_fractions}."
        )

    if uniqueness_risk_columns:
        return ProbeResult(
            conclusive=False,
            reason=(
                "uniqueness_saturation_risk: "
                f"{sorted(uniqueness_risk_columns)!r} approach their full-scale "
                "cardinality bound, invisible to a small-N probe (§11)"
            ),
        )
    if opaque_generator_tables:
        return ProbeResult(
            conclusive=False,
            reason=(
                f"opaque/nonlinear generator tables {sorted(opaque_generator_tables)!r}: "
                "small-N behavior is not representative of full-scale behavior"
            ),
        )

    points: list[ProbePoint] = []
    for fraction in (low_frac, high_frac):
        point_or_result = _run_one_probe(
            config,
            sources,
            reference_table=reference_table,
            fraction=fraction,
            floor_rows=floor_rows,
            run_isolated=run_isolated,
            mem_cap_bytes=mem_cap_bytes,
            timeout_s=timeout_s,
            run_pipeline_kwargs=run_pipeline_kwargs,
        )
        if isinstance(point_or_result, ProbeResult):
            return point_or_result
        points.append(point_or_result)
    low_point, high_point = points

    if low_point.rows == high_point.rows:
        return ProbeResult(
            conclusive=False,
            reason=(
                f"degenerate probe pair: both scales floored to the same "
                f"{low_point.rows} rows -- no slope is measurable (reduce "
                "floor_rows or widen probe_fractions)"
            ),
            low_point=low_point,
            high_point=high_point,
        )

    fit = _fit_line(low_point, high_point)
    if fit is None:
        return ProbeResult(
            conclusive=False,
            reason="degenerate probe points (non-increasing rows)",
            low_point=low_point,
            high_point=high_point,
        )
    slope, intercept = fit
    if slope <= 0:
        return ProbeResult(
            conclusive=False,
            reason=(
                f"non-positive measured slope ({slope!r} bytes/row) -- noisy "
                "or nonlinear probe measurement"
            ),
            low_point=low_point,
            high_point=high_point,
        )

    estimated = slope * target_rows + intercept
    if estimated < 0:
        return ProbeResult(
            conclusive=False,
            reason=f"extrapolated estimate is negative ({estimated!r}); fit is not trustworthy",
            low_point=low_point,
            high_point=high_point,
        )

    return ProbeResult(
        conclusive=True,
        reason="measured",
        estimated_peak_bytes=round(estimated),
        slope_bytes_per_row=slope,
        intercept_bytes=intercept,
        low_point=low_point,
        high_point=high_point,
    )


def probe_fits(result: ProbeResult, budget_bytes: int, *, error_band: float = 0.30) -> bool | None:
    """Whether the probe's extrapolated peak clears `budget_bytes` with the
    same asymmetric margin `_mem_estimate.fits` applies.

    Returns `None` -- not `False` -- when `result.conclusive` is False:
    "the probe could not measure this" is a distinct claim from "confirmed
    does not fit," and coercing it to `False` would (harmlessly, since both
    route bounded downstream) hide WHY, and (less harmlessly) invites a
    future caller to treat `None`/`False` as interchangeable when they are
    not: `None` here still allows a DIFFERENT recovery mechanism (a future
    tighter probe, telemetry) to try again; a confirmed `False` should not
    be retried. Mirrors `_mem_estimate.fits`'s exact `None`-vs-`False`
    contract.
    """
    if error_band < 0:
        raise ValueError(f"error_band must be >= 0, got {error_band}.")
    if budget_bytes <= 0:
        raise ValueError(f"budget_bytes must be positive, got {budget_bytes}.")
    if not result.conclusive:
        return None
    estimated_bytes = result.estimated_peak_bytes
    if estimated_bytes is None:
        raise AssertionError(
            "ProbeResult.conclusive is True but estimated_peak_bytes is None; "
            "ProbeResult.__post_init__ should have rejected this construction."
        )
    return estimated_bytes * (1 + error_band) < budget_bytes

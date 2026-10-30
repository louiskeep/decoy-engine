"""Recalibration (`recalibrate_k`) and the in-memory store for the B5 loop.

Split out of `mem_telemetry.py` (see its docstring for the loop's safety
properties); `mem_telemetry_records.py` holds the record type and builders.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from mem_telemetry_records import MemoryTelemetryOutcome, MemoryTelemetryRecord

from decoy_engine.execution._mem_estimate import ExecutionPath

__all__ = ["KRecalibration", "MemoryTelemetryStore", "recalibrate_k"]

# `outcome` values `recalibrate_k` never counts as evidence -- mirrors
# `_MEMORY_MISS_TRIP_KINDS`'s exclusion of non-memory failures.
_NON_EVIDENCE_OUTCOMES: frozenset[MemoryTelemetryOutcome] = frozenset({"crashed"})


# ---------------------------------------------------------------------------
# Recalibration (§3.4 -- THE safety-critical part)
# ---------------------------------------------------------------------------

# Halved from each path's cold-start constant (`_mem_estimate.py`): a
# documented, hard floor no recalibration may ever cross, regardless of what
# the telemetry says. Cutting a conservative cold-start constant in half is
# itself a large, deliberately-visible concession -- not a number derived
# from any measurement -- so that adopting it still requires the telemetry
# gates (min-sample + margin) to fire, and even then the module refuses to
# go any lower than this. Widening this floor further requires a documented,
# reviewed decision, not a telemetry-only signal.
_K_FLOOR_DEFAULT: dict[ExecutionPath, float] = {
    "full_frame": 1.5,
    "out_of_core": 1.0,
    "sequential": 0.75,
}

_DEFAULT_MIN_SAMPLES_FOR_LOWER = 20
_DEFAULT_LOWER_MARGIN = 0.15
_DEFAULT_PERCENTILE = 1.0  # true max -- see module docstring safety property 2.

RecalibrationDirection = Literal["raise", "lower", "hold"]


@dataclass(frozen=True)
class KRecalibration:
    """A SUGGESTED per-path `k`, never a live mutation of `_mem_estimate`'s
    module constants (§3.4: "a SUGGESTION the operator/config adopts, not a
    silent live mutation").

    `direction` names which of the three cases fired. `gates_passed` is
    `True` for `"raise"` (always adopted immediately) and for `"lower"`
    (only when the min-sample + margin + floor gates all held); it is
    `False` whenever `direction == "hold"` because a lowering was rejected
    outright, and it is also `False` when `sample_count == 0` (nothing to
    suggest at all -- `suggested_k` is then just `current_k` echoed back
    unchanged).
    """

    path: ExecutionPath
    current_k: float
    suggested_k: float
    sample_count: int
    direction: RecalibrationDirection
    gates_passed: bool
    floor_k: float
    percentile: float


def _percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy's default method).

    `pct=1.0` (this module's default) is the exact maximum: `idx` reduces to
    `len(values) - 1`, i.e. the last sorted element, so "max" is not a
    special case here -- it falls out of the general formula.
    """
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = pct * (len(ordered) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    frac = idx - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def recalibrate_k(
    records: Sequence[MemoryTelemetryRecord],
    path: ExecutionPath,
    *,
    current_k: float,
    floor_k: float | None = None,
    min_samples_for_lower: int = _DEFAULT_MIN_SAMPLES_FOR_LOWER,
    percentile: float = _DEFAULT_PERCENTILE,
    lower_margin: float = _DEFAULT_LOWER_MARGIN,
    schema_fingerprint: str | None = None,
) -> KRecalibration:
    """Compute a suggested `k` for `path` from `records` (§3.4).

    Conservative by construction, in the OOM-safe direction (module
    docstring):

      1. Filters to `isolated == True` records for `path`, excludes any
         `outcome` in `_NON_EVIDENCE_OUTCOMES` (currently `"crashed"` --
         see `MemoryTelemetryRecord`'s docstring), and (if
         `schema_fingerprint` is given) further restricts to that one shape.
         An excluded record is dropped from the sample count and the
         percentile entirely -- it is not weak evidence, it is not evidence.
      2. Aggregates the filtered `observed_slope` (INTERCEPT-REMOVED per-byte
         slope, comparable to the pinned `K_<route>_SLOPE` -- module docstring
         safety property 2) at `percentile` -- never a mean or median.
         `percentile` MUST be exactly `1.0` (the true max); see the
         `percentile` arg doc for why this is enforced rather than left as a
         caller-tunable knob. `current_k` is the pinned slope in force (a
         `K_<route>_SLOPE`), so the comparison is slope-vs-slope.
      3. Raising (`percentile_slope > current_k`) is adopted immediately: no
         minimum sample count, no margin -- under-shooting the slope is the
         one unacceptable error.
      4. Lowering (`percentile_slope < current_k`) is gated: `sample_count`
         must be `>= min_samples_for_lower`, AND `percentile_slope * (1 +
         lower_margin) < current_k`, so a same-ballpark measurement never
         trims the constant on noise alone. Either gate failing means
         `direction="hold"` and `suggested_k == current_k`.
      5. The suggestion is NEVER allowed below `safety_bound = max(floor_k,
         true_max_observed_slope)`, clamped on BOTH the raise and lower paths
         -- so neither a floor breach nor an under-shoot of the pool's own
         observed maximum slope can slip through even if every other gate
         mistakenly passed (HIGH remediation: enforced in code, not
         delegated to `percentile`).

    Args:
        records: telemetry pool. Not required to be pre-filtered -- all of
            `path`/`isolated`/`outcome`/`schema_fingerprint` filtering is
            this function's own job.
        path: the execution path to recalibrate.
        current_k: the constant in force today (e.g.
            `_mem_estimate.K_FULL_FRAME_SLOPE`).
        floor_k: overrides `_K_FLOOR_DEFAULT[path]`. Must be `<= current_k`
            (a floor above the constant already in force is a
            contradiction).
        min_samples_for_lower: minimum sample count before ANY lowering is
            considered. Never gates a raise.
        percentile: MUST be exactly `1.0` (the true max observed `k`). Kept
            for API stability but validated, not trusted: see the module
            docstring's safety property 2.
        lower_margin: required fractional headroom between the observed
            high-percentile `k` and `current_k` before a lowering is
            adopted.
        schema_fingerprint: if given, further restricts the sample to this
            one schema shape rather than every schema that has hit `path`.

    Returns:
        A `KRecalibration` -- a suggestion, never a mutation of any live
        constant.

    Raises:
        ValueError: `percentile != 1.0`, `lower_margin < 0`,
            `min_samples_for_lower < 1`, or `floor_k` (resolved) is greater
            than `current_k`.
    """
    if percentile != 1.0:
        raise ValueError(
            f"percentile must be exactly 1.0, got {percentile}. Sub-max aggregation "
            "dilutes the one dangerous sample this loop exists to respect and can "
            "suggest a k below a schema's true worst observed case; kept for API "
            "stability but pinned to the true maximum."
        )
    if lower_margin < 0:
        raise ValueError(f"lower_margin must be >= 0, got {lower_margin}.")
    if min_samples_for_lower < 1:
        raise ValueError(f"min_samples_for_lower must be >= 1, got {min_samples_for_lower}.")
    resolved_floor = floor_k if floor_k is not None else _K_FLOOR_DEFAULT[path]
    if resolved_floor > current_k:
        raise ValueError(
            f"floor_k={resolved_floor} is greater than current_k={current_k}; a floor "
            "above the constant already in force is a contradiction."
        )

    filtered = [
        record
        for record in records
        if record.path == path
        and record.isolated
        and record.outcome not in _NON_EVIDENCE_OUTCOMES
        and (schema_fingerprint is None or record.schema_fingerprint == schema_fingerprint)
    ]
    sample_count = len(filtered)
    if sample_count == 0:
        return KRecalibration(
            path=path,
            current_k=current_k,
            suggested_k=current_k,
            sample_count=0,
            direction="hold",
            gates_passed=False,
            floor_k=resolved_floor,
            percentile=percentile,
        )

    # INTERCEPT-REMOVED slopes, not raw `observed_k` point ratios (module
    # docstring safety property 2): the quantity comparable to `current_k`
    # (a pinned slope) is the slope with the route's fixed intercept removed.
    observed_slopes = [record.observed_slope for record in filtered]
    true_max_observed_slope = max(observed_slopes)
    percentile_slope = _percentile(observed_slopes, percentile)
    # Hard backstop (HIGH remediation): computed independently of
    # `percentile_slope`, applied on every return path below.
    safety_bound = max(resolved_floor, true_max_observed_slope)

    if percentile_slope > current_k:
        # Raising is immediate -- no gates, no sample-count floor.
        suggested = max(percentile_slope, safety_bound)
        return KRecalibration(
            path=path,
            current_k=current_k,
            suggested_k=suggested,
            sample_count=sample_count,
            direction="raise",
            gates_passed=True,
            floor_k=resolved_floor,
            percentile=percentile,
        )

    if percentile_slope < current_k:
        enough_samples = sample_count >= min_samples_for_lower
        margin_cleared = percentile_slope * (1 + lower_margin) < current_k
        gates_passed = enough_samples and margin_cleared
        if not gates_passed:
            return KRecalibration(
                path=path,
                current_k=current_k,
                suggested_k=current_k,
                sample_count=sample_count,
                direction="hold",
                gates_passed=False,
                floor_k=resolved_floor,
                percentile=percentile,
            )
        # Clamped to the safety bound even after the gates pass: the floor
        # (and the pool's own true max) are a hard backstop, not merely
        # another gate that can be satisfied away.
        suggested = max(percentile_slope, safety_bound)
        direction: RecalibrationDirection = "lower" if suggested < current_k else "hold"
        return KRecalibration(
            path=path,
            current_k=current_k,
            suggested_k=suggested,
            sample_count=sample_count,
            direction=direction,
            gates_passed=True,
            floor_k=resolved_floor,
            percentile=percentile,
        )

    # percentile_slope == current_k exactly: nothing to change.
    return KRecalibration(
        path=path,
        current_k=current_k,
        suggested_k=current_k,
        sample_count=sample_count,
        direction="hold",
        gates_passed=True,
        floor_k=resolved_floor,
        percentile=percentile,
    )


# ---------------------------------------------------------------------------
# In-memory aggregation (§4: "the actual STORE (DB) is platform, deferred ...
# provide the record + a simple in-memory aggregation the platform can back
# with persistence")
# ---------------------------------------------------------------------------


class MemoryTelemetryStore:
    """The simplest possible telemetry sink: an in-memory list plus
    `recalibrate`. Deliberately NOT a database -- persistence is platform's
    job (§4) -- but the interface (`add`/`all`/`recalibrate`) is shaped so a
    platform-side, DB-backed implementation can drop in behind the same
    three calls without this module's callers needing to change.
    """

    def __init__(self) -> None:
        self._records: list[MemoryTelemetryRecord] = []

    def add(self, record: MemoryTelemetryRecord) -> None:
        self._records.append(record)

    def all(self) -> tuple[MemoryTelemetryRecord, ...]:
        return tuple(self._records)

    def recalibrate(
        self,
        path: ExecutionPath,
        *,
        current_k: float,
        floor_k: float | None = None,
        min_samples_for_lower: int = _DEFAULT_MIN_SAMPLES_FOR_LOWER,
        percentile: float = _DEFAULT_PERCENTILE,
        lower_margin: float = _DEFAULT_LOWER_MARGIN,
        schema_fingerprint: str | None = None,
    ) -> KRecalibration:
        return recalibrate_k(
            self._records,
            path,
            current_k=current_k,
            floor_k=floor_k,
            min_samples_for_lower=min_samples_for_lower,
            percentile=percentile,
            lower_margin=lower_margin,
            schema_fingerprint=schema_fingerprint,
        )

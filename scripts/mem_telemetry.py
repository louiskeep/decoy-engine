"""Sprint B5: the TELEMETRY self-calibration loop (OOM-avoidance routing
redesign, `docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §3.4,
corrected by §11's §3.4 erratum and refined by §13).

Calibration tooling, not engine runtime: no engine, platform or CLI code
imports it, so it lives in `scripts/` beside `tb4_calibration.py` (the harness
that pins the constants this loop suggests changes to) and, like that script,
reads the estimator's own `decoy_engine.execution._mem_estimate` helpers.
Tests import it as `mem_telemetry` through pytest's `scripts` pythonpath. This
file is the stable import surface and the home of the design notes; the code
lives in `mem_telemetry_records.py` (record, fingerprint, builders) and
`mem_telemetry_calibration.py` (`recalibrate_k`, the store).

This module replaces nothing -- `K_FULL_FRAME_SLOPE` and its siblings
in `_mem_estimate.py` stay exactly as they are; this loop only computes a
*suggestion* an operator/config can choose to adopt in their place. Wiring
a suggestion back into `_K_PATH` is deliberately out of scope here, same as
every other B-series sprint's "additive primitive, production wiring is a
separate step" shape.

Two non-negotiable safety properties, both load-bearing per §11's telemetry
erratum and §13's k-is-not-schema-invariant finding:

  1. **Only `isolated=True` records may feed a recalibration.** A process-wide
     `ru_maxrss`/contaminated peak (the in-process fallback, or any pre-1a
     warm-worker sample) is, per §11, a HISTORICAL high-water mark across an
     unknown number of prior jobs in the same process -- it can only ever
     read HIGH relative to any single job's own peak, never accurately, and
     folding it in would silently and monotonically inflate `k` upward with
     no floor on how wrong that inflation is. `recalibrate_k` filters on
     `MemoryTelemetryRecord.isolated` before it looks at anything else.
  2. **The drift signal is the INTERCEPT-REMOVED per-byte slope, not the raw
     point ratio.** `estimate_peak_bytes` now predicts `intercept + raw_bytes
     * K_<route>_SLOPE` (`_mem_estimate.py`, TB-5 precondition #72), so the
     through-origin point ratio `actual_peak_bytes / raw_bytes` is
     STRUCTURALLY inflated by the fixed intercept at small basis: a job that
     fits the model reads a ratio ABOVE the slope (numeric_fk full_frame @500k:
     858/191.7 = 4.48 > the 4.0 slope) purely because the intercept was not
     subtracted, so comparing it to the pinned slope would spuriously raise on
     EVERY small job and ratchet the slope up on nothing. `recalibrate_k`
     therefore aggregates `observed_slope = max(0, (actual_peak_bytes - route
     intercept) / raw_bytes)`, removing the SAME per-route intercept the
     estimator added (`route_intercept_bytes`), and compares THAT against
     `current_k` (the pinned slope) -- same units, so a raise fires only on a
     genuine slope drift (peak growing FASTER per byte), not on intercept
     inflation.

  3. **Aggregation is the maximum observed slope, never a mean or median.**
     `observed_slope` is only a lower bound on the true per-byte multiplier
     for a giant schema class in the general case, and the ONE failure mode
     this loop must never produce is a slope that under-shoots a future job's
     real peak (§13: "the naive calibration errs in the OOM direction" is
     exactly the mistake being guarded against here). The true maximum -- one
     wide/unique-string/numeric-heavy job, or one governor trip -- pins the
     suggestion at that sample's level regardless of how many low,
     pooled-string samples surround it; a mean or median would dilute it into
     the noise floor instead. `recalibrate_k` PINS `percentile` to `1.0`
     (rejects any other value) and hard-clamps `suggested_k` to never fall
     below the pool's own true `max(observed_slope)` on every return path --
     the invariant is enforced in code, not left for a caller-tunable
     aggregation knob to preserve.

Raising `k` (the estimate needs to go UP to stay safe) is adopted the
instant the evidence supports it -- no minimum sample count, no margin.
LOWERING `k` (claiming a schema needs LESS conservative headroom than the
current constant) requires the opposite: a minimum sample count
(`min_samples_for_lower`), a margin between the observed high-percentile and
the current constant (`lower_margin`) so a same-ballpark measurement never
trims the constant on noise alone, and a hard floor (`floor_k`,
`_K_FLOOR_DEFAULT`) the suggestion can never cross regardless of what the
telemetry says. This mirrors §3.6's asymmetric-margin logic in `_mem_estimate.
fits`: an unsafe miss (OOM) is unacceptable; a safe miss (leaving a job on a
more conservative constant than it strictly needed) only costs wall-clock,
so the gates apply to exactly one direction.
"""

from __future__ import annotations

from mem_telemetry_calibration import (
    KRecalibration,
    MemoryTelemetryStore,
    _percentile,  # noqa: F401  (importable for tests; deliberately not in __all__)
    recalibrate_k,
)
from mem_telemetry_records import (
    MemoryTelemetryOutcome,
    MemoryTelemetryRecord,
    schema_fingerprint,
    telemetry_record_from_governor_trip,
    telemetry_record_from_isolated_run,
)

__all__ = [
    "KRecalibration",
    "MemoryTelemetryOutcome",
    "MemoryTelemetryRecord",
    "MemoryTelemetryStore",
    "recalibrate_k",
    "schema_fingerprint",
    "telemetry_record_from_governor_trip",
    "telemetry_record_from_isolated_run",
]

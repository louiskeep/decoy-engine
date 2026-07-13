"""Sprint B5: the TELEMETRY self-calibration loop (OOM-avoidance routing
redesign, `docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §3.4,
corrected by §11's §3.4 erratum and refined by §13).

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
  2. **Aggregation is the maximum observed `k`, never a mean or median.**
     `k = actual_peak_bytes / raw_bytes` is only a lower bound on the true
     multiplier for a giant schema class in the general case, and the ONE
     failure mode this loop must never produce is a `k` that under-shoots a
     future job's real peak (§13: "the naive calibration errs in the OOM
     direction" is exactly the mistake being guarded against here). The
     true maximum -- one wide/unique-string/numeric-heavy job, or one
     governor trip -- pins the suggestion at that sample's level regardless
     of how many low, pooled-string samples surround it; a mean or median
     would dilute it into the noise floor instead. `recalibrate_k` PINS
     `percentile` to `1.0` (rejects any other value) and hard-clamps
     `suggested_k` to never fall below the pool's own true `max(observed_k)`
     on every return path -- the invariant is enforced in code, not left for
     a caller-tunable aggregation knob to preserve.

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

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from decoy_engine.execution._mem_estimate import ColumnSizeSpec, ExecutionPath, TableSizeSpec

if TYPE_CHECKING:
    from decoy_engine.execution._governor import GovernorTripRecord
    from decoy_engine.execution._isolated_common import IsolatedRunResult

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

MemoryTelemetryOutcome = Literal["completed", "self_oom", "governor_trip", "crashed"]


# ---------------------------------------------------------------------------
# The record type (§3.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryTelemetryRecord:
    """One completed-or-tripped job's predicted-vs-actual peak, on the SAME
    `raw_bytes` basis `_mem_estimate.estimate_peak_bytes` used to produce
    `predicted_bytes` (§3.4: "compares predicted vs actual for the SAME
    raw_bytes basis").

    `schema_fingerprint` (`schema_fingerprint()` below) lets records for the
    same schema SHAPE aggregate together regardless of row count -- k is a
    per-shape ratio, not a per-row-count one (§1's "dead-linear in rows"
    finding is exactly why a ratio, not a fresh calibration per row count,
    is the right model).

    `isolated` is carried through unchanged from `IsolatedRunResult.isolated`
    / the governor's own isolation guarantee -- see the module docstring's
    safety property 1. `outcome` records WHY this sample exists for
    diagnostics; `recalibrate_k` filters `outcome == "crashed"` out of every
    recalibration (a process that died of an unrelated bug at low RSS never
    demonstrated the schema "fits" there -- mirrors `_MEMORY_MISS_TRIP_KINDS`'s
    exclusion of the same failure class for governor trips). The other three
    outcomes all count; `self_oom`/`governor_trip` naturally produce a HIGH
    `k = actual_peak_bytes / raw_bytes`, which the max aggregation surfaces
    on its own.
    """

    schema_fingerprint: str
    path: ExecutionPath
    raw_bytes: int
    predicted_bytes: int
    actual_peak_bytes: int
    isolated: bool
    outcome: MemoryTelemetryOutcome

    def __post_init__(self) -> None:
        if self.raw_bytes <= 0:
            raise ValueError(f"raw_bytes must be > 0, got {self.raw_bytes}.")
        if self.predicted_bytes < 0:
            raise ValueError(f"predicted_bytes must be >= 0, got {self.predicted_bytes}.")
        if self.actual_peak_bytes < 0:
            raise ValueError(f"actual_peak_bytes must be >= 0, got {self.actual_peak_bytes}.")

    @property
    def observed_k(self) -> float:
        """`actual_peak_bytes / raw_bytes` -- this record's own contribution
        to a `k_path` recalibration. Always defined: `raw_bytes > 0` is
        enforced by `__post_init__`.
        """
        return self.actual_peak_bytes / self.raw_bytes


# ---------------------------------------------------------------------------
# Schema fingerprint (§3.4: "schema fingerprint")
# ---------------------------------------------------------------------------

# Width buckets a variable-width column's priced byte-width falls into.
# Boundaries, not exact widths, are what the fingerprint hashes: two samples
# of the SAME underlying column distribution (e.g. two profiling runs at
# different row counts) can measure slightly different average widths
# without crossing a bucket boundary, so the fingerprint stays stable for
# what is genuinely the same schema shape -- exactly the row-count-
# independence property §3.4 requires, extended to sampling noise on top of
# row count.
_WIDTH_CLASS_BOUNDARIES: tuple[int, ...] = (8, 16, 32, 64, 128, 256, 512, 1024)


def _width_class(width_bytes: float | None) -> str:
    if width_bytes is None:
        return "unpriceable"
    for boundary in _WIDTH_CLASS_BOUNDARIES:
        if width_bytes <= boundary:
            return f"<={boundary}"
    return f">{_WIDTH_CLASS_BOUNDARIES[-1]}"


def _column_shape(column: ColumnSizeSpec) -> tuple[str, str]:
    return (column.dtype, _width_class(column.string_width_bytes))


def schema_fingerprint(
    tables: Sequence[TableSizeSpec], *, fk_edges: Sequence[tuple[str, str]] = ()
) -> str:
    """A stable hash over the schema SHAPE: table count, per-column dtype +
    string-width class, and FK structure -- deliberately NOT row counts, so
    the same shape at different scales (the exact thing k is invariant
    across, per §1's linearity finding) fingerprints identically.

    `fk_edges` is `(child_table_name, parent_table_name)` pairs. Table NAMES
    themselves are not part of the hash (a fingerprint describes shape, not
    identity) -- `fk_edges` is resolved to table-POSITION pairs (via each
    name's index in `tables`) before hashing, so renaming every table the
    same way leaves the fingerprint unchanged while a genuinely different FK
    graph (a different set of position-pairs) does not.

    Raises:
        ValueError: an `fk_edges` entry names a table not present in
            `tables` -- a caller error (mismatched inputs), not a case to
            silently ignore.
    """
    name_to_index = {table.name: index for index, table in enumerate(tables)}
    table_shapes = tuple(
        tuple(_column_shape(column) for column in table.columns) for table in tables
    )
    fk_positions: list[tuple[int, int]] = []
    for child_name, parent_name in fk_edges:
        for role, name in (("child", child_name), ("parent", parent_name)):
            if name not in name_to_index:
                raise ValueError(
                    f"schema_fingerprint: fk_edges references {role} table {name!r}, "
                    "which is not present in `tables`."
                )
        fk_positions.append((name_to_index[child_name], name_to_index[parent_name]))
    canonical = repr((len(tables), table_shapes, tuple(sorted(fk_positions))))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Emission helpers (§3.4: build a record from a real run or a governor trip)
# ---------------------------------------------------------------------------


def telemetry_record_from_isolated_run(
    result: IsolatedRunResult,
    *,
    schema_fingerprint: str,
    path: ExecutionPath,
    raw_bytes: int,
    predicted_bytes: int,
    mem_cap_bytes: int | None = None,
) -> MemoryTelemetryRecord:
    """Build a `MemoryTelemetryRecord` from a `run_pipeline_isolated` result.

    `result.isolated` is carried through unchanged -- an in-process fallback
    run (§12 ruling 2, "option c") produces a contaminated `peak_rss_mb`
    that this function still wraps into a record (visible for diagnostics),
    but `isolated=False` means `recalibrate_k` filters it out before it
    contributes to a `k` suggestion -- see the module docstring's safety
    property 1.

    `result.outcome` maps to three different treatments (MEDIUM remediation
    fixing the asymmetry this used to have with
    `telemetry_record_from_governor_trip`): `"completed"` -> `"completed"`
    as-is; `"oom_killed"` -> `"self_oom"`, with `actual_peak_bytes` FLOORED
    at `mem_cap_bytes` (when given) since a self-OOM means the true peak
    was AT LEAST the cap that triggered it, even if the last sample came in
    under it; `"crashed"` -> `"crashed"`, still recorded for diagnostics but
    excluded from every `recalibrate_k` sample (see `_NON_EVIDENCE_OUTCOMES`).

    Raises:
        ValueError: `result.peak_rss_mb` is `None` -- the run never reported
            a peak at all (e.g. a hard external kill the child could not
            self-report before dying), and there is nothing trustworthy to
            record. Do not guess a value here; skip emitting a record for
            that run instead.
    """
    if result.peak_rss_mb is None:
        raise ValueError(
            "telemetry_record_from_isolated_run: result.peak_rss_mb is None -- this "
            "run never reported a peak (e.g. an unrecoverable abnormal exit) and "
            "there is nothing trustworthy to record."
        )
    reported_peak_bytes = int(result.peak_rss_mb * 1024 * 1024)
    outcome: MemoryTelemetryOutcome
    actual_peak_bytes: int
    if result.outcome == "completed":
        outcome = "completed"
        actual_peak_bytes = reported_peak_bytes
    elif result.outcome == "oom_killed":
        outcome = "self_oom"
        actual_peak_bytes = (
            max(reported_peak_bytes, mem_cap_bytes)
            if mem_cap_bytes is not None
            else reported_peak_bytes
        )
    else:  # "crashed" -- not a memory-miss kind, see docstring above.
        outcome = "crashed"
        actual_peak_bytes = reported_peak_bytes
    return MemoryTelemetryRecord(
        schema_fingerprint=schema_fingerprint,
        path=path,
        raw_bytes=raw_bytes,
        predicted_bytes=predicted_bytes,
        actual_peak_bytes=actual_peak_bytes,
        isolated=result.isolated,
        outcome=outcome,
    )


# Trip kinds that represent a genuine memory-estimate miss: the estimator
# said this route would fit and it did not. `route_ineligible` (the next
# rung on the ladder was never a candidate for this job's shape) and
# `crashed` (a non-memory failure -- see `_governor.py`'s own handling) are
# NOT memory misses; folding either into `k` would push the multiplier up
# for a reason that has nothing to do with the estimator's byte math being
# wrong, corrupting `k` on unrelated noise.
_MEMORY_MISS_TRIP_KINDS = frozenset({"governor_kill", "self_oom"})

# `outcome` values `recalibrate_k` never counts as evidence -- mirrors
# `_MEMORY_MISS_TRIP_KINDS`'s exclusion of non-memory failures.
_NON_EVIDENCE_OUTCOMES: frozenset[MemoryTelemetryOutcome] = frozenset({"crashed"})


def telemetry_record_from_governor_trip(
    trip: GovernorTripRecord,
    *,
    schema_fingerprint: str,
    raw_bytes: int,
    predicted_bytes: int,
) -> MemoryTelemetryRecord:
    """Build a `MemoryTelemetryRecord` from a `GovernorTripRecord` (§3.4:
    "Governor trips ... feed in as observations where actual_peak >= budget
    at the kill point").

    A trip is, by construction, a route the estimator predicted would fit
    and that instead was killed at or above its budget -- the estimate
    under-predicted. `actual_peak_bytes` is therefore floored at
    `trip.budget_bytes` even when the monitor's last-sampled
    `observed_peak_mb` came in slightly under it (poll-cadence lag between
    samples, `_governor.py`'s `poll_interval_s`): the true peak at the
    moment of the kill was AT LEAST the hard threshold that triggered it,
    never less, so this function never under-reports what pushed the trip.
    This is precisely the "governor trips push k UP" property the
    recalibration test suite pins.

    Raises:
        ValueError: `trip.trip_kind` is not a memory-miss kind
            (`_MEMORY_MISS_TRIP_KINDS`) -- a `route_ineligible` or `crashed`
            trip is not evidence the estimator under-predicted memory, and
            must not be fed into a `k` recalibration.
    """
    if trip.trip_kind not in _MEMORY_MISS_TRIP_KINDS:
        raise ValueError(
            f"telemetry_record_from_governor_trip: trip_kind={trip.trip_kind!r} is not "
            f"a memory-miss kind ({sorted(_MEMORY_MISS_TRIP_KINDS)}) -- a route-"
            "ineligibility or an unrelated crash is not evidence the memory estimate "
            "under-predicted, and must not feed a k recalibration."
        )
    observed_bytes = (
        int(trip.observed_peak_mb * 1024 * 1024) if trip.observed_peak_mb is not None else 0
    )
    actual_peak_bytes = max(observed_bytes, trip.budget_bytes)
    return MemoryTelemetryRecord(
        schema_fingerprint=schema_fingerprint,
        path=trip.route,
        raw_bytes=raw_bytes,
        predicted_bytes=predicted_bytes,
        actual_peak_bytes=actual_peak_bytes,
        # The governor only ever supervises an isolated (subprocess) child --
        # `run_job_with_governor` raises before spawning anything if
        # `isolate=False` is passed (`_governor.py`'s `_validate_call`), so a
        # trip is always a clean per-job VmRSS sample, never a contaminated
        # process-wide one.
        isolated=True,
        outcome="governor_trip",
    )


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
      2. Aggregates the filtered `observed_k` at `percentile` -- never a
         mean or median. `percentile` MUST be exactly `1.0` (the true max);
         see the `percentile` arg doc for why this is enforced rather than
         left as a caller-tunable knob.
      3. Raising (`percentile_k > current_k`) is adopted immediately: no
         minimum sample count, no margin -- under-shooting `k` is the one
         unacceptable error.
      4. Lowering (`percentile_k < current_k`) is gated: `sample_count`
         must be `>= min_samples_for_lower`, AND `percentile_k * (1 +
         lower_margin) < current_k`, so a same-ballpark measurement never
         trims the constant on noise alone. Either gate failing means
         `direction="hold"` and `suggested_k == current_k`.
      5. The suggestion is NEVER allowed below `safety_bound = max(floor_k,
         true_max_observed_k)`, clamped on BOTH the raise and lower paths --
         so neither a floor breach nor an under-shoot of the pool's own
         observed maximum can slip through even if every other gate
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

    observed_ks = [record.observed_k for record in filtered]
    true_max_observed_k = max(observed_ks)
    percentile_k = _percentile(observed_ks, percentile)
    # Hard backstop (HIGH remediation): computed independently of
    # `percentile_k`, applied on every return path below.
    safety_bound = max(resolved_floor, true_max_observed_k)

    if percentile_k > current_k:
        # Raising is immediate -- no gates, no sample-count floor.
        suggested = max(percentile_k, safety_bound)
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

    if percentile_k < current_k:
        enough_samples = sample_count >= min_samples_for_lower
        margin_cleared = percentile_k * (1 + lower_margin) < current_k
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
        suggested = max(percentile_k, safety_bound)
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

    # percentile_k == current_k exactly: nothing to change.
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

"""Telemetry record, schema fingerprint and record builders for the B5 loop.

Split out of `mem_telemetry.py` (see its docstring for the loop's safety
properties); `mem_telemetry_calibration.py` holds the recalibration half.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from decoy_engine.execution._mem_estimate import (
    ColumnSizeSpec,
    ExecutionPath,
    FkCardinalityInput,
    TableSizeSpec,
    estimator_basis_bytes,
    route_intercept_bytes,
)

if TYPE_CHECKING:
    from decoy_engine.execution._governor import GovernorTripRecord
    from decoy_engine.execution._isolated_common import IsolatedRunResult

__all__ = [
    "MemoryTelemetryOutcome",
    "MemoryTelemetryRecord",
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

    #74: `raw_bytes` here is the ESTIMATOR BASIS
    (`_mem_estimate.estimator_basis_bytes`), which for the SEQUENTIAL route is
    the WORKING SET (two largest tables + FK dedup), NOT total raw bytes --
    `observed_slope` divides by it, so it must match the basis
    `route_slope(path)` multiplies or the sequential slope is silently
    under-stated (OOM-unsafe). The `telemetry_record_from_*` builders enforce
    this via `_assert_basis_matches_estimator`; a caller constructing this
    dataclass directly owns the same contract.

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
        """`actual_peak_bytes / raw_bytes` -- the raw THROUGH-ORIGIN point
        ratio. Kept for diagnostics/provenance ONLY: it is intercept-inflated
        at small basis (see `observed_slope`) and is NOT what `recalibrate_k`
        aggregates. Always defined: `raw_bytes > 0` is enforced by
        `__post_init__`.
        """
        return self.actual_peak_bytes / self.raw_bytes

    @property
    def observed_slope(self) -> float:
        """The INTERCEPT-REMOVED per-byte slope `recalibrate_k` aggregates:
        `max(0, (actual_peak_bytes - route intercept) / raw_bytes)`.

        `estimate_peak_bytes` predicts `intercept + raw_bytes * slope`, so the
        signal comparable to the pinned `K_<route>_SLOPE` is the slope with the
        fixed intercept subtracted -- NOT the `observed_k` point ratio, which
        the intercept inflates at small basis (module docstring safety property
        2). The subtracted intercept is exactly the one the estimator added for
        this record's route (`route_intercept_bytes`), so the two never desync.
        Floored at `0.0`: a peak entirely under the fixed intercept shows no
        per-byte growth, and a negative slope is not a safe drift signal.
        """
        residual = self.actual_peak_bytes - route_intercept_bytes(self.path)
        return max(0.0, residual / self.raw_bytes)


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


def _assert_basis_matches_estimator(
    *,
    path: ExecutionPath,
    raw_bytes: int,
    tables: Sequence[TableSizeSpec] | None,
    fk_cardinality: FkCardinalityInput | None,
) -> None:
    """#74 basis contract: guard that `raw_bytes` (the divisor
    `MemoryTelemetryRecord.observed_slope` uses) is the SAME basis
    `estimate_peak_bytes` multiplied by `route_slope(path)` for this run.

    The SEQUENTIAL route is the footgun: its estimator basis is the WORKING
    SET (two largest tables + FK dedup, `_mem_estimate.estimator_basis_bytes`),
    NOT total raw bytes. A telemetry wiring that passes total raw bytes for a
    sequential run divides `observed_slope` by a basis LARGER than the one the
    pinned `K_SEQUENTIAL_SLOPE` was calibrated against, UNDER-stating the
    observed slope -- the OOM-unsafe direction (the drift detector would never
    fire a raise and the estimator would keep under-predicting sequential
    peak). So `tables` is REQUIRED for a sequential record and the basis is
    verified against `estimator_basis_bytes`; for the in-core/out-of-core
    routes the basis equals total raw bytes, and the check is applied too when
    `tables` is supplied.

    Wiring status (#74): TB-5 flips the routing flags to default-ON but does
    NOT wire the live drift loop -- that stays platform-owned. This guard is
    the standing contract the platform wiring must satisfy when it does; it is
    a no-op for the in-core routes when a caller omits `tables` (back-compat),
    and MANDATORY for sequential so the wrong-basis footgun cannot ship
    silently.
    """
    if path == "sequential" and tables is None:
        raise ValueError(
            "telemetry basis contract (#74): a sequential-route record must pass "
            "`tables` (and `fk_cardinality` when the job has an FK dedup set) so "
            "`raw_bytes` can be verified as the WORKING-SET basis "
            "`estimator_basis_bytes(tables, 'sequential', ...)` returns -- NOT total "
            "raw bytes. Dividing observed_slope by total raw bytes under-states the "
            "sequential slope, the OOM-unsafe direction."
        )
    if tables is None:
        return
    basis = estimator_basis_bytes(tables, path, fk_cardinality=fk_cardinality)
    if basis.basis_bytes is None:
        # UNPRICEABLE: the estimator could not have produced a numeric
        # prediction either, so there is no basis to check against.
        return
    if raw_bytes != basis.basis_bytes:
        raise ValueError(
            f"telemetry basis contract (#74): raw_bytes={raw_bytes} does not match the "
            f"estimator basis {basis.basis_bytes} for path={path!r} "
            f"(estimator_basis_bytes). observed_slope divides by raw_bytes, so it must "
            f"be the SAME basis route_slope() multiplies -- for sequential that is the "
            f"working set (two largest tables + FK dedup), not total raw bytes."
        )


def telemetry_record_from_isolated_run(
    result: IsolatedRunResult,
    *,
    schema_fingerprint: str,
    path: ExecutionPath,
    raw_bytes: int,
    predicted_bytes: int,
    mem_cap_bytes: int | None = None,
    tables: Sequence[TableSizeSpec] | None = None,
    fk_cardinality: FkCardinalityInput | None = None,
) -> MemoryTelemetryRecord:
    """Build a `MemoryTelemetryRecord` from a `run_pipeline_isolated` result.

    `raw_bytes` is the ESTIMATOR BASIS (`_mem_estimate.estimator_basis_bytes`),
    NOT necessarily total raw bytes: for the SEQUENTIAL route the basis is the
    working set (two largest tables + FK dedup). `tables` (and `fk_cardinality`)
    are the #74 basis contract -- REQUIRED for a sequential record so the basis
    is verified against `estimator_basis_bytes`, optional for the in-core /
    out-of-core routes (whose basis is total raw bytes); see
    `_assert_basis_matches_estimator`.

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
    _assert_basis_matches_estimator(
        path=path, raw_bytes=raw_bytes, tables=tables, fk_cardinality=fk_cardinality
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


def telemetry_record_from_governor_trip(
    trip: GovernorTripRecord,
    *,
    schema_fingerprint: str,
    raw_bytes: int,
    predicted_bytes: int,
    tables: Sequence[TableSizeSpec] | None = None,
    fk_cardinality: FkCardinalityInput | None = None,
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
    # #74 basis contract: the governor ladder reroutes THROUGH sequential, so a
    # trip on the sequential rung is a sequential record and must use the
    # working-set basis, not total raw bytes (see `_assert_basis_matches_estimator`).
    _assert_basis_matches_estimator(
        path=trip.route, raw_bytes=raw_bytes, tables=tables, fk_cardinality=fk_cardinality
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

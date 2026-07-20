"""Phase-aware DuckDB memory sizing plus the hybrid capacity preflight for
the out-of-core FK route.

Split out from `_budget.py` rather than appended there, the same move
`_spill_estimate.py` already made for the disk-side estimator: `_budget.py`
is within a handful of lines of the 600-LOC orchestration cap (CLAUDE.md
"Engineering best practices"), so a sibling module holds the new surface
instead of forcing a decomposition mid-sprint.

TWO THINGS LIVE HERE, ONE ROOT CAUSE EACH FIXES:

1. PHASE-AWARE CAPS (`memory_limit_for` / `resolve_phase_memory_limits`).
   `resolve_ooc_memory_limit` (`_budget.py`) resolves ONE `memory_limit`
   string, divided by the run's GLOBAL WORST-CASE concurrency
   (`_max_concurrent_ooc_instances`), and `_runner.py` used to thread that
   single string into every DuckDB connection it opens. But DuckDB instances
   on this route open and close in PHASES with different LOCAL liveness: a
   table's incoming-edge joiners are co-live with each other, but the SINK
   path always closes them (`_emit.py`'s `on_stream_consumed`) before that
   table's own outgoing relation build opens -- so the build is the ONLY
   live instance at that moment, not two. Dividing every phase by the global
   peak starves any phase whose LOCAL live count is below that peak; a
   measured 100M-row/4GB cloud run OOMed inside DuckDB with half its budget
   idle for exactly this reason (a 1-instance build handed a divided-by-2
   cap). `memory_limit_for` is the single place a byte budget becomes one
   phase's per-connection cap; `resolve_phase_memory_limits` derives the
   three caps `_runner.py` needs for one table (joiner / sink-path build /
   resident-path build) from its own incoming-edge count, which is the one
   piece of information this module intentionally does not carry on its own
   (like `_budget.py`, it takes plain counts from its caller rather than
   depending on `decoy_engine.relationships`).

2. THE HYBRID CAPACITY PREFLIGHT (`predict_ooc_build_floor_bytes` /
   `enforce_ooc_memory_preflight`). Phase-aware caps LOWER the floor but
   cannot always eliminate it: a big enough parent table's relation-build
   dedup still needs real non-spillable resident state (hash-aggregate
   control structures, allocator overhead) that DuckDB cannot push to
   `temp_directory` no matter how generous the `memory_limit`. Cam's
   decision (governing goal) is a HYBRID gate: warn near that floor, hard-fail
   beyond it, so a job that cannot fit is refused BEFORE any DuckDB work runs
   rather than left to OOM mid-job. This is the never-crash guarantee; item 1
   above is the floor-lowering fix underneath it.

   THE GATE MUST COMPARE AGAINST THE EXACT CAP THE BUILD WILL GET, NOT A
   FRACTION OF THE RAW MEMORY CEILING. The two are different numbers:
   `resolve_ooc_memory_limit` (`_budget.py`) already subtracts a reserve from
   the ceiling before DuckDB ever sees a byte (Python/Arrow/OS overhead), and
   item 1's phase-aware division then splits THAT budget again by per-table
   liveness. A preflight that instead re-derives its own fraction of the raw
   ceiling can pass a job whose real, phase-aware cap is starved -- the exact
   BLOCKER this sprint's remediation closes (a 100M-row/4GB run was ADMITTED
   by a ceiling-fraction check, then OOMed inside DuckDB's own accounting,
   because the fraction and the real cap were never the same number). So
   `enforce_ooc_memory_preflight` takes `budget_bytes` -- THE SAME
   `OutOfCoreBudget.budget_bytes` `run_out_of_core_route` resolves at its one
   `resolve_ooc_memory_limit` call site, reused rather than re-derived -- plus
   sink-ness and each candidate table's own incoming-edge count, and computes
   `cap(t)` with the identical arithmetic `resolve_phase_memory_limits` uses
   to size the real connection: `budget_bytes` undivided on the sink path (the
   build is the only live instance once joiners close), `budget_bytes //
   (incoming_edges(t) + 1)` on the resident path (joiners stay open through
   the build). The warn/fail fractions below then multiply THAT cap, never
   the raw ceiling -- see `enforce_ooc_memory_preflight`'s docstring for why
   this preflight is a HARD gate where the sibling
   `_spill_estimate.enforce_ooc_disk_preflight` is advisory-only (disk spill
   has a soft failure mode; a resident-memory floor above the cap does not).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._budget import (
    _OOC_RESERVE_FLOOR_BYTES,
    _OOC_RESERVE_FRACTION,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "MemoryPreflight",
    "declared_minimum_ceiling_bytes",
    "enforce_ooc_memory_preflight",
    "memory_limit_for",
    "predict_ooc_build_floor_bytes",
    "resolve_phase_memory_limits",
]


def memory_limit_for(budget_bytes: int, live_instances: int) -> str:
    """One DuckDB connection's `memory_limit` for a phase where
    `live_instances` connections are co-live, sized so the SUM of every live
    instance's cap never exceeds `budget_bytes`.

    Same strict-floor division `resolve_ooc_memory_limit` (`_budget.py`)
    already established, exposed as a free function so a caller with
    phase-local liveness (not the run's single global-peak concurrency
    number) can size each connection at the point it opens. NOT floored up
    at any per-instance minimum: that would over-subscribe a tight budget
    split across high fan-in, which is the over-commit the whole invariant
    exists to forbid -- the overflow spills to `temp_directory` instead. The
    lone exception is a sub-1-MiB split (only reachable at >64-way fan-in on
    a near-floor ~64 MiB budget), where DuckDB rejects a literal "0MB" limit
    outright, so the string floors at "1MB"; the sum can then nominally
    exceed `budget_bytes`, but spilling, not the cap, carries the run in
    that regime.
    """
    if live_instances < 1:
        raise ExecutionError(
            code="out_of_core_concurrency_invalid",
            message=f"live_instances must be >= 1, got {live_instances}.",
        )
    per_instance_mib = max(1, budget_bytes // live_instances // (1024 * 1024))
    # Same MiB-with-decimal-suffix rounding-down rationale as `_budget.py`:
    # DuckDB reads "MB" as base-10, so the effective limit lands slightly
    # below the MiB byte count -- the safe direction, never over the cap.
    return f"{per_instance_mib}MB"


def resolve_phase_memory_limits(
    *,
    budget_bytes: int | None,
    memory_limit: str | None,
    incoming_edges: int,
) -> tuple[str | None, str | None, str | None, str | None]:
    """`(sink_joiner, resident_joiner, sink_build, resident_build)`
    memory_limit strings for one table's incoming-edge joiners and its own
    outgoing relation build.

    `budget_bytes` is the UNDIVIDED per-run budget (`OutOfCoreBudget.
    budget_bytes`, threaded through as `run_fk_out_of_core`'s own
    `budget_bytes` param). `None` means host-RAM detection failed and no
    explicit budget was given (the same fall-through `resolve_ooc_memory_
    limit` already documents) -- every phase then falls back to the flat
    `memory_limit` string, reproducing pre-phase-aware-cap behavior exactly
    rather than inventing a budget to divide.

    Phase-local liveness (the module docstring's item 1) differs by path, so
    the JOINER cap must too -- a connection's `memory_limit` is fixed at
    open, and a joiner that stays live into the build phase needs the SAME
    cap the build itself gets, not the cap sized for its own (narrower)
    phase. SINK path: joiners are co-live with each other only (cap = budget
    // incoming_edges), and always close before that table's own outgoing
    build opens (`_emit.py`'s `on_stream_consumed`), so the build is the
    run's ONLY live instance at that moment (cap = budget, undivided -- the
    fix for the measured 100M/4GB OOM). RESIDENT (no-sink) path: joiners stay
    open THROUGH the build, so both must open at the SAME cap = budget //
    (incoming_edges + 1) -- the build counts as one more live instance
    alongside its own joiners. Returning one flat `joiner` value used on both
    paths (the pre-fix shape) let the resident path's joiners open at the
    sink-sized (undivided-by-the-extra-build-slot) cap while co-live with a
    build sized for the extra slot, so their SUM exceeded `budget_bytes` --
    exactly the HIGH this split closes: co-live sum on the resident path is
    now `(incoming_edges + 1) * (budget // (incoming_edges + 1)) <=
    budget_bytes`, honoring the invariant every other phase-aware cap in this
    module already holds. A table with zero incoming edges opens no joiner at
    all, so both joiner values are simply unused by its caller.
    """
    if budget_bytes is None:
        return memory_limit, memory_limit, memory_limit, memory_limit
    sink_joiner = memory_limit_for(budget_bytes, incoming_edges) if incoming_edges else memory_limit
    resident_joiner = (
        memory_limit_for(budget_bytes, incoming_edges + 1) if incoming_edges else memory_limit
    )
    sink_build = memory_limit_for(budget_bytes, 1)
    resident_build = memory_limit_for(budget_bytes, incoming_edges + 1)
    return sink_joiner, resident_joiner, sink_build, resident_build


# --- Part B: hybrid capacity preflight -------------------------------------

# WARN band lower edge, as a fraction of the ACTUAL per-table build cap
# `cap(t)` (never the raw memory ceiling -- see the module docstring's
# remediation note). `cap(t)` is already the exact byte count
# `resolve_phase_memory_limits` hands the real DuckDB connection, so a floor
# at or below it should, in principle, complete; the WARN band exists only
# because `predict_ooc_build_floor_bytes` is itself a conservative model, not
# because `cap(t)` carries slack of its own -- the HARD-FAIL bound is the
# full cap (fraction 1.0: `floor(t) > cap(t)`, no additional margin), since
# `cap(t)` IS the number that will starve the build, not an estimate of it.
_OOC_MEM_WARN_FRACTION = 0.6

# The relation-build dedup's non-spillable floor, per row of the largest
# parent table (see `predict_ooc_build_floor_bytes`'s docstring for the
# derivation). This model is a CONSERVATIVE UPPER ENVELOPE over two datasets
# that disagree by ~4x, fit so it never under-predicts EITHER (the module's
# governing rule: an under-prediction admits a job that then OOMs).
#
# Dataset 1 -- CLEAN isolated devbox sweep (`scripts/build_floor_probe.py`,
# `build_parent_key_relation` run directly on int64-keyed parent.parquet,
# RLIMIT_DATA FIXED at 8000 MiB across EVERY tier so a failure is
# attributable to `memory_limit` alone, never the rlimit). This removes the
# prior calibration's confound: its 2048 MiB-FAIL anchor used RLIMIT_DATA
# 3000 MiB and bad_alloc'd at peak_total_mem 1329 MiB -- BELOW its own cap,
# an rlimit/fragmentation artifact, not a memory_limit floor. The clean
# per-row-count floor bracket (highest FAIL, lowest PASS] in MiB, scanned
# across a WIDE tier range because DuckDB's memory_limit does not always fail
# monotonically -- a larger limit can pick a worse partition count and fail
# where a smaller limit passed, e.g. 1M below):
#   100k rows -> (32, 48]     20M rows -> (256, 384]
#   1M rows   -> (96, 128]    40M rows -> FAILS at 512 (floor > 512 MiB;
#   5M rows   -> (64, 80]                the pass edge was not cleanly narrowed)
#   10M rows  -> (128, 192]
# The clean isolated floor is roughly linear at only ~13-19 MiB per M-rows.
#
# Dataset 2 -- REAL-ROUTE cloud measurement (fk_memory_probe out_of_core, GCP
# n2-standard-8, 33.3M-row parent per table). The 33.3M-row BUILD phase OOMed
# at memory_limit ~1638 MiB and COMPLETED at 2457 MiB: floor(33.3M) in
# (~1638, 2457]. This is ~4x higher per-row than the isolated devbox sweep
# predicts, because the isolated probe strips away everything the real route's
# build coexists with (the streamed sink pipeline, orphan handling, wider
# staged payloads, cross-environment allocator fragmentation). The 33.3M/4GB
# real-route OOM is THE motivating failure this whole preflight exists to
# refuse; it is a valid observed floor (a real memory_limit OOM, NOT an rlimit
# artifact), so "never under-predict any observed floor" FORCES the model to
# clear it. The isolated sweep is therefore a LOWER witness (the model must
# over-predict it, which it does by ~4-10x); the cloud point is the BINDING
# upper anchor.
#
# FIT: base + slope. SLOPE is anchored on the cloud passing edge (2457 MiB at
# 33.3M rows, ~70 MiB/M-row net of base) with a documented 1.5x safety
# envelope for the ~4x isolated-vs-real spread and cross-env fragmentation ->
# ~105 MiB/M-row = 110 bytes/row. The SAFETY of the never-OOM guarantee rests
# on the SLOPE (it dominates at the row counts where real OOMs occur), so the
# slope over-predicts every large-row observed floor: floor(33.3M) ~= 3517 MiB
# over-predicts the cloud 2457 completion level (never under-predicts the real
# route), and it stays STRICTLY ABOVE the highest FAILING memory_limit tier at
# EVERY clean devbox row count (100k..40M), which is the exact property that
# makes admitting a job (cap >= floor) safe: cap then clears every tier known
# to OOM at that row count. The two acceptance points stay REFUSED at a 4 GiB
# host (build cap 2048 MiB after the reserve): floor(20M) ~= 2122 MiB > 2048,
# floor(100M) ~= 10.3 GiB > 2048.
_BUILD_FLOOR_BYTES_PER_ROW = 110.0

# BASE is the fixed DuckDB relation-build overhead at ~zero rows, and unlike
# the slope it does NOT carry the never-OOM guarantee (real OOMs are a
# large-row, slope-dominated regime). It is deliberately SMALL -- 24 MiB, below
# the 32 MiB highest-FAIL tier at 100k rows -- for one structural reason: the
# preflight gates `floor(t)` against the ACTUAL build cap, and the smallest
# real cap is `_MIN_BUDGET_BYTES` (64 MiB) divided by phase liveness (as low as
# 32 MiB on a resident fan-in-1 build under the route's 64 MiB byte-estimate
# routing knob). A larger base would refuse a genuinely tiny job (tens of rows)
# whose real floor is a few MiB -- and byte-transparency for those jobs is a
# hard requirement (`tests/parity/test_out_of_core_*_routing.py`, 40-row
# fixtures under the 64 MiB knob). 24 MiB keeps `floor(40 rows) ~= 24 MiB`
# under that 32 MiB cap while the SLOPE still lifts `floor(100k) ~= 34.5 MiB`
# strictly above 100k's own 32 MiB fail tier. A near-zero parent still opens
# one real DuckDB instance, so the floor never predicts near zero.
_BUILD_FLOOR_BASE_BYTES = 24 * 1024 * 1024


def predict_ooc_build_floor_bytes(max_parent_rows: int) -> int:
    """The conservative, data-independent non-spillable resident floor for
    the out-of-core route's relation-build phase, given a parent table's row
    count.

    Parent ROW COUNT (not distinct-key count) is the input because it is
    available pre-run from routing signals alone, and distinct parent keys
    can never exceed parent rows -- so pricing off rows is a safe upper
    bound on the relation's true cardinality, never an under-count.

    MODEL: `_BUILD_FLOOR_BASE_BYTES + _BUILD_FLOOR_BYTES_PER_ROW *
    max_parent_rows`. This prices the NON-SPILLABLE floor: the part of
    DuckDB's larger-than-memory relation build (hash-aggregate control
    structures for the last-write-wins GROUP BY dedup, plus allocator
    overhead) that stays resident no matter how much of the working set
    spills to `temp_directory` (DuckDB "Memory Management" / "Tuning
    Workloads": `memory_limit` bounds the buffer manager, and operator state
    spills past it, but a query's control structures do not shrink below
    some genuine minimum). See `_BUILD_FLOOR_BYTES_PER_ROW`'s own docstring
    comment for the exact measured anchors this constant was fit against
    (never re-derive those established points; recalibrate only the
    slope/base here, and only with new measured data).

    Bias to over-predict throughout (module docstring, item 2): an
    under-prediction lets a job through that then OOMs mid-run, which is the
    one outcome the hybrid preflight exists to prevent; an over-prediction
    only refuses a job that might have completed. `max(0, max_parent_rows)`
    guards a caller-supplied negative row count from producing a smaller
    (wrong-direction) floor.
    """
    rows = max(0, max_parent_rows)
    return _BUILD_FLOOR_BASE_BYTES + int(_BUILD_FLOOR_BYTES_PER_ROW * rows)


@dataclass(frozen=True)
class MemoryPreflight:
    """Result of the hybrid out-of-core memory capacity check.

    Covers every parent table `enforce_ooc_memory_preflight` was given, not
    one row count: `binding_table` names whichever table drove the
    `ok`/`warned` outcome (the argmax of `floor(t) - cap(t)` on a hard-fail;
    the tightest warn-band table otherwise), and `floor_bytes` / `cap_bytes`
    are THAT table's own numbers, not a job-wide aggregate -- the invariant
    this gate enforces is per-table (`_memory_estimate` module docstring).
    `binding_table` is `None` when there is nothing to report: no parent
    tables at all, or a clean run with no warning.

    `detectable=False` marks the fail-open case: no real `budget_bytes` to
    gate against (host-RAM detection failed and no explicit budget was
    supplied), matching the same fall-through `resolve_ooc_memory_limit`
    already documents for Part A's phase-aware caps -- the caller proceeds
    rather than inventing a cap to check floors against.
    """

    ok: bool
    warned: bool
    detectable: bool
    binding_table: str | None
    floor_bytes: int
    cap_bytes: int | None


def declared_minimum_ceiling_bytes(floor_bytes: int, *, incoming_edges: int, sink: bool) -> int:
    """The smallest whole-GiB memory ceiling that, fed back through
    `resolve_ooc_memory_limit`, yields a build cap >= `floor_bytes` -- the
    truthful "you need approximately N GB" a hard-fail or warning reports.

    Inverts `resolve_ooc_memory_limit`'s OWN subtractive reserve model
    (`budget = ceiling - max(_OOC_RESERVE_FRACTION * ceiling,
    _OOC_RESERVE_FLOOR_BYTES)`), reading that function's actual constants
    rather than a fresh approximation, so the number this returns is the
    exact one `resolve_ooc_memory_limit` will honor if the recommendation is
    provisioned (a preflight that inverted a DIFFERENT model than the one
    Part A actually runs is the same class of denomination mismatch this
    sprint's remediation closes elsewhere):

    - `ceiling` small enough that the reserve is the flat floor
      (`_OOC_RESERVE_FRACTION * ceiling <= _OOC_RESERVE_FLOOR_BYTES`):
      `budget = ceiling - _OOC_RESERVE_FLOOR_BYTES`, so
      `ceiling = budget + _OOC_RESERVE_FLOOR_BYTES`.
    - otherwise the reserve is the fraction: `budget = (1 -
      _OOC_RESERVE_FRACTION) * ceiling`, so `ceiling = budget / (1 -
      _OOC_RESERVE_FRACTION)`.
    The two solutions agree exactly where the regimes meet
    (`_OOC_RESERVE_FRACTION * ceiling == _OOC_RESERVE_FLOOR_BYTES`), so
    picking whichever regime the UNROUNDED small-regime candidate is
    self-consistent with (computed first, only then rounded up) never
    straddles the boundary.

    `required_budget` is `floor_bytes` on the sink path (the build gets the
    undivided budget) but `floor_bytes * (incoming_edges + 1)` on the
    resident path: `resolve_phase_memory_limits` divides the resident
    build's own connection by that same divisor, so a ceiling that only
    clears the UNDIVIDED floor would still starve the build once resolved
    back through the real phase-aware cap.
    """
    required_budget = floor_bytes if sink else floor_bytes * (incoming_edges + 1)
    small_regime_ceiling = required_budget + _OOC_RESERVE_FLOOR_BYTES
    if _OOC_RESERVE_FRACTION * small_regime_ceiling <= _OOC_RESERVE_FLOOR_BYTES:
        ceiling_needed_bytes = small_regime_ceiling
    else:
        ceiling_needed_bytes = math.ceil(required_budget / (1 - _OOC_RESERVE_FRACTION))
    gib = 1024**3
    whole_gib = -(-ceiling_needed_bytes // gib)  # ceil to a whole GiB, never under
    return whole_gib * gib


def enforce_ooc_memory_preflight(
    parent_table_rows: Mapping[str, int],
    *,
    budget_bytes: int | None,
    sink: bool,
    incoming_edge_counts: Mapping[str, int],
) -> MemoryPreflight:
    """The out-of-core route's HYBRID (warn near the floor, hard-fail beyond
    it) memory capacity gate -- Cam's governing decision (module docstring
    item 2): "never OOM, or declare minimums so the user knows what power
    they need."

    Deliberately asymmetric with the sibling disk preflight
    (`_spill_estimate.enforce_ooc_disk_preflight`, advisory/warn-only): that
    guard's failure mode is soft (an under-predicted disk estimate still hits
    the runtime `check_temp_disk_budget` backstop and aborts cleanly at a
    table boundary), so a hard reject up front would needlessly kill jobs a
    conservative over-estimate merely made LOOK tight. A resident-memory
    floor above its actual cap has no such backstop -- DuckDB's own internal
    allocator raises a raw "bad allocation" partway through a query, not a
    coded, catchable error at a clean boundary -- so THIS preflight is the
    only guard standing between an admitted job and an uncontrolled crash,
    and it must actually reject rather than merely warn.

    `parent_table_rows` is one row count per table with an outgoing FK edge
    (a build-phase table); `budget_bytes` is the SAME `OutOfCoreBudget.
    budget_bytes` `resolve_ooc_memory_limit` resolved at the route's one call
    site (reused, never re-derived -- the module docstring's remediation
    note); `sink` is whether this run streams to a sink; `incoming_edge_counts`
    is each table's own fan-in. For every table `t`:

        floor(t) = predict_ooc_build_floor_bytes(parent_table_rows[t])
        cap(t)   = budget_bytes                          if sink
                 = budget_bytes // (incoming_edge_counts[t] + 1)   otherwise

    the SAME arithmetic `resolve_phase_memory_limits` uses to size the real
    connection. HARD-FAILS (`ExecutionError(code=
    "out_of_core_insufficient_memory")`, before any DuckDB work) if
    `floor(t) > cap(t)` for ANY `t` -- the binding table is `argmax_t
    (floor(t) - cap(t))`. Otherwise WARNS (never blocks) if `floor(t) >=
    _OOC_MEM_WARN_FRACTION * cap(t)` for any table, picking the tightest such
    table for the message. `budget_bytes=None` (host-RAM detection failed and
    no explicit budget was given, mirroring `resolve_ooc_memory_limit`'s own
    fall-through) fails OPEN: Part A's phase-aware caps fall back to the flat
    `memory_limit` in this case too, so there is no real per-table cap left
    to gate a floor against.
    """
    if budget_bytes is None:
        return MemoryPreflight(
            ok=True,
            warned=False,
            detectable=False,
            binding_table=None,
            floor_bytes=0,
            cap_bytes=None,
        )

    worst_fail: tuple[int, str, int, int] | None = None  # (margin, table, floor, cap)
    worst_warn: tuple[int, str, int, int] | None = None
    for table, rows in parent_table_rows.items():
        floor_bytes = predict_ooc_build_floor_bytes(rows)
        incoming = incoming_edge_counts.get(table, 0)
        cap_bytes = budget_bytes if sink else budget_bytes // (incoming + 1)
        margin = floor_bytes - cap_bytes
        if worst_fail is None or margin > worst_fail[0]:
            worst_fail = (margin, table, floor_bytes, cap_bytes)
        if floor_bytes >= _OOC_MEM_WARN_FRACTION * cap_bytes:
            if worst_warn is None or margin > worst_warn[0]:
                worst_warn = (margin, table, floor_bytes, cap_bytes)

    if worst_fail is not None and worst_fail[0] > 0:
        _, table, floor_bytes, cap_bytes = worst_fail
        incoming = incoming_edge_counts.get(table, 0)
        floor_gib = floor_bytes / (1024**3)
        cap_gib = cap_bytes / (1024**3)
        needed_gib = declared_minimum_ceiling_bytes(
            floor_bytes, incoming_edges=incoming, sink=sink
        ) / (1024**3)
        raise ExecutionError(
            code="out_of_core_insufficient_memory",
            message=(
                f"predicted resident floor ~{floor_gib:.2f} GiB for table {table!r} exceeds "
                f"the actual build cap ~{cap_gib:.2f} GiB it would receive; this job needs "
                f"approximately {needed_gib:.0f} GB of memory (a host/cgroup ceiling that size). "
                "Increase host/cgroup memory or reduce table size."
            ),
        )

    if worst_warn is None:
        return MemoryPreflight(
            ok=True,
            warned=False,
            detectable=True,
            binding_table=None,
            floor_bytes=0,
            cap_bytes=budget_bytes,
        )

    _, table, floor_bytes, cap_bytes = worst_warn
    incoming = incoming_edge_counts.get(table, 0)
    floor_gib = floor_bytes / (1024**3)
    cap_gib = cap_bytes / (1024**3)
    recommend_gib = declared_minimum_ceiling_bytes(
        floor_bytes, incoming_edges=incoming, sink=sink
    ) / (1024**3)
    _logger.warning(
        "out-of-core memory advisory: predicted resident floor ~%.2f GiB for table %r "
        "(actual build cap ~%.2f GiB); recommend a host/cgroup ceiling of >= %.0f GB for margin.",
        floor_gib,
        table,
        cap_gib,
        recommend_gib,
    )
    return MemoryPreflight(
        ok=True,
        warned=True,
        detectable=True,
        binding_table=table,
        floor_bytes=floor_bytes,
        cap_bytes=cap_bytes,
    )

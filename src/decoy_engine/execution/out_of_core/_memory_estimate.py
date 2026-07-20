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
   beyond a safe bound, so a job that cannot fit is refused BEFORE any DuckDB
   work runs rather than left to OOM mid-job. This is the never-crash
   guarantee; item 1 above is the floor-lowering fix underneath it. See
   `enforce_ooc_memory_preflight`'s docstring for why this preflight is a
   HARD gate where the sibling `_spill_estimate.enforce_ooc_disk_preflight`
   is advisory-only -- the two guard different failure modes (disk spill has
   a soft failure mode; a resident-memory floor above the ceiling does not).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._budget import detect_effective_memory_bytes

_logger = logging.getLogger(__name__)

__all__ = [
    "MemoryPreflight",
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
) -> tuple[str | None, str | None, str | None]:
    """`(joiner, sink_build, resident_build)` memory_limit strings for one
    table's incoming-edge joiners and its own outgoing relation build.

    `budget_bytes` is the UNDIVIDED per-run budget (`OutOfCoreBudget.
    budget_bytes`, threaded through as `run_fk_out_of_core`'s own
    `budget_bytes` param). `None` means host-RAM detection failed and no
    explicit budget was given (the same fall-through `resolve_ooc_memory_
    limit` already documents) -- every phase then falls back to the flat
    `memory_limit` string, reproducing pre-phase-aware-cap behavior exactly
    rather than inventing a budget to divide.

    Phase-local liveness (the module docstring's item 1): a table's
    `incoming_edges` joiners are co-live with each other (cap = budget //
    incoming_edges). The SINK path always closes them before that table's
    own outgoing build opens (`_emit.py`'s `on_stream_consumed`), so that
    build is the run's ONLY live instance at that moment (cap = budget,
    undivided -- the fix for the measured 100M/4GB OOM). The RESIDENT
    (no-sink) path keeps the joiners open through the build (cap = budget //
    (incoming_edges + 1), since the build itself is one more live instance
    alongside them). A table with zero incoming edges opens no joiner at
    all, so the returned joiner value is simply unused by its caller.
    """
    if budget_bytes is None:
        return memory_limit, memory_limit, memory_limit
    joiner = memory_limit_for(budget_bytes, incoming_edges) if incoming_edges else memory_limit
    sink_build = memory_limit_for(budget_bytes, 1)
    resident_build = memory_limit_for(budget_bytes, incoming_edges + 1)
    return joiner, sink_build, resident_build


# --- Part B: hybrid capacity preflight -------------------------------------

# WARN band lower edge and HARD-FAIL lower edge, both fractions of the
# detected ceiling. The gap between them (0.6 - 0.85) is deliberate runway: a
# predicted floor that just clears the warn threshold still has real margin
# for the reserve (Python/Arrow/OS) that lives outside DuckDB's own
# accounting, the same reserve `resolve_ooc_memory_limit`'s subtractive model
# carves out explicitly. The SAFE bound sits below 1.0, not at it, for the
# same reason: 100% of the ceiling leaves nothing for that reserve.
_OOC_MEM_WARN_FRACTION = 0.6
_OOC_MEM_SAFE_FRACTION = 0.85

# The relation-build dedup's non-spillable floor, per row of the largest
# parent table (see `predict_ooc_build_floor_bytes`'s docstring for the
# derivation). Anchored from a devbox acceptance probe sweep (SPRINT-1 B4,
# `scratchpad/oocfloor/build_probe.py` against a real 20M-row parent.parquet,
# pinned DuckDB `memory_limit`, RLIMIT_DATA safety-capped, real duckdb_memory()
# sampling) bracketing the true per-run floor at 20M rows:
#   256 MiB  -> FAILS  (HASH_TABLE peak ~221 MiB / ~244 MiB usable)
#   512 MiB  -> FAILS  (HASH_TABLE peak ~429 MiB / ~488 MiB usable)
#   1024 MiB -> FAILS  (HASH_TABLE peak ~769 MiB / ~977 MiB usable)
#   2048 MiB -> FAILS  (HASH_TABLE peak ~1017 MiB, allocator ~327 MiB --
#                       fails on a single 256 KiB allocation well BELOW the
#                       nominal cap, so allocator/fragmentation overhead
#                       grows with the row count too, not just HASH_TABLE)
#   3276 MiB -> COMPLETES (the pre-established undivided-budget measurement:
#                       peak_total_mem 1829 MiB, HASH_TABLE peak 1344 MiB)
# So the true floor at 20M rows sits in (2048, 3276] MiB. The constant below
# prices max_parent_rows=20M at ~2893 MiB -- above every failing tier with
# real margin, and closer to the known-good 3276 MiB than to the 2048 MiB
# failure, biasing to over-predict (this module's governing goal: an
# under-prediction lets a job through only to OOM, the one outcome the
# preflight exists to prevent; over-prediction only refuses a job that MIGHT
# have completed).
_BUILD_FLOOR_BYTES_PER_ROW = 145.0

# Fixed per-run overhead below which the model does not shrink: DuckDB's own
# baseline instance overhead (allocator bookkeeping, buffer manager state)
# independent of row count, anchored to the measured ~113-128 MiB one-instance
# floor at moderate row counts (the 12M-row join-phase floor `_budget.py`'s
# module docstring also cites). A near-zero parent table (few or zero rows)
# still opens one real DuckDB instance, so the floor never predicts near zero.
_BUILD_FLOOR_BASE_BYTES = 128 * 1024 * 1024


def predict_ooc_build_floor_bytes(max_parent_rows: int) -> int:
    """The conservative, data-independent non-spillable resident floor for
    the out-of-core route's relation-build phase, given the largest parent
    row count across the job's outgoing FK edges.

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
    (never re-derive those two established points; recalibrate only the
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

    `floor_bytes` is `predict_ooc_build_floor_bytes`'s prediction;
    `ceiling_bytes` is the detected effective memory ceiling (or the
    caller-supplied override); `warn_bytes` / `safe_bound_bytes` are the two
    fraction-of-ceiling thresholds the floor was compared against.
    `detectable=False` marks the fail-open case (an undetectable ceiling):
    `ok` is then `True` and no floor/bound comparison was made, matching the
    host-RAM-detection fall-through `resolve_ooc_memory_limit` already
    documents -- the caller proceeds rather than inventing a floor to check
    against a ceiling it does not have.
    """

    ok: bool
    warned: bool
    detectable: bool
    floor_bytes: int
    ceiling_bytes: int | None
    warn_bytes: int | None
    safe_bound_bytes: int | None


def enforce_ooc_memory_preflight(
    max_parent_rows: int,
    *,
    ceiling_bytes: int | None = None,
) -> MemoryPreflight:
    """The out-of-core route's HYBRID (warn near the floor, hard-fail beyond
    a safe bound) memory capacity gate -- Cam's governing decision (module
    docstring item 2): "never OOM, or declare minimums so the user knows
    what power they need."

    Deliberately asymmetric with the sibling disk preflight
    (`_spill_estimate.enforce_ooc_disk_preflight`, advisory/warn-only): that
    guard's failure mode is soft (an under-predicted disk estimate still hits
    the runtime `check_temp_disk_budget` backstop and aborts cleanly at a
    table boundary), so a hard reject up front would needlessly kill jobs a
    conservative over-estimate merely made LOOK tight. A resident-memory
    floor above the safe bound has no such backstop -- DuckDB's own internal
    allocator raises a raw "bad allocation" partway through a query, not a
    coded, catchable error at a clean boundary -- so THIS preflight is the
    only guard standing between an admitted job and an uncontrolled crash,
    and it must actually reject rather than merely warn.

    Computes `floor = predict_ooc_build_floor_bytes(max_parent_rows)` and
    compares it against `ceiling_bytes` (or `detect_effective_memory_bytes()`
    when omitted):

    - `floor < _OOC_MEM_WARN_FRACTION * ceiling`: comfortably clear, no
      warning, no raise.
    - `_OOC_MEM_WARN_FRACTION * ceiling <= floor < _OOC_MEM_SAFE_FRACTION *
      ceiling`: WARN band. Logs a structured, actionable message (predicted
      floor, available ceiling, a recommended minimum) via the same
      `logging`-based advisory channel `enforce_ooc_disk_preflight` already
      uses for this route -- observability only, the job proceeds.
    - `floor >= _OOC_MEM_SAFE_FRACTION * ceiling`: HARD-FAIL. Raises a typed
      `ExecutionError(code="out_of_core_insufficient_memory", ...)` BEFORE
      any DuckDB work runs, stating the declared minimum (module docstring's
      "declare minimums" half of the governing goal) so the caller knows
      what power to provision instead of discovering it via a crash.

    Fail-open ONLY when the ceiling itself is undetectable (`ceiling_bytes`
    omitted and `detect_effective_memory_bytes()` raises): mirrors the
    host-RAM-detection fall-through `resolve_ooc_memory_limit` already takes
    for the same undetectable-ceiling case, rather than inventing a floor to
    compare against a ceiling that was never measured. This is the only
    fail-open path; once a ceiling is in hand (detected or caller-supplied),
    the hard-fail branch above always actually rejects.
    """
    floor_bytes = predict_ooc_build_floor_bytes(max_parent_rows)
    if ceiling_bytes is None:
        try:
            ceiling_bytes = detect_effective_memory_bytes()
        except ExecutionError:
            return MemoryPreflight(
                ok=True,
                warned=False,
                detectable=False,
                floor_bytes=floor_bytes,
                ceiling_bytes=None,
                warn_bytes=None,
                safe_bound_bytes=None,
            )
    warn_bytes = int(_OOC_MEM_WARN_FRACTION * ceiling_bytes)
    safe_bound_bytes = int(_OOC_MEM_SAFE_FRACTION * ceiling_bytes)
    if floor_bytes >= safe_bound_bytes:
        floor_gib = floor_bytes / (1024**3)
        safe_gib = safe_bound_bytes / (1024**3)
        ceiling_gib = ceiling_bytes / (1024**3)
        # The declared minimum: enough headroom over the predicted floor to
        # clear the safe bound with margin, rounded up to a whole GiB so the
        # message states an actionable, provisionable number.
        needed_gib = -(-int(floor_bytes / _OOC_MEM_SAFE_FRACTION) // (1024**3))
        raise ExecutionError(
            code="out_of_core_insufficient_memory",
            message=(
                f"predicted resident floor ~{floor_gib:.2f} GiB exceeds the safe bound "
                f"(~{safe_gib:.2f} GiB = {_OOC_MEM_SAFE_FRACTION:.0%} of ~{ceiling_gib:.2f} "
                f"GiB available); this job needs approximately {needed_gib} GB of memory. "
                "Increase host/cgroup memory or reduce table size."
            ),
        )
    warned = floor_bytes >= warn_bytes
    if warned:
        floor_gib = floor_bytes / (1024**3)
        ceiling_gib = ceiling_bytes / (1024**3)
        recommend_gib = -(-int(floor_bytes / _OOC_MEM_SAFE_FRACTION) // (1024**3))
        _logger.warning(
            "out-of-core memory advisory: predicted resident floor ~%.2f GiB for this "
            "host (~%.2f GiB available); recommend >= %d GB for margin.",
            floor_gib,
            ceiling_gib,
            recommend_gib,
        )
    return MemoryPreflight(
        ok=True,
        warned=warned,
        detectable=True,
        floor_bytes=floor_bytes,
        ceiling_bytes=ceiling_bytes,
        warn_bytes=warn_bytes,
        safe_bound_bytes=safe_bound_bytes,
    )

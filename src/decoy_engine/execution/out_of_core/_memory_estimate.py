"""Phase-aware DuckDB memory sizing, plus the row-based build-floor model and
ceiling-recommendation math the capacity verdict layer (`_capacity_eval.py`)
prices jobs against.

Split out from `_budget.py` rather than appended there, the same move
`_spill_estimate.py` already made for the disk-side estimator: `_budget.py`
is within a handful of lines of the 600-LOC orchestration cap (CLAUDE.md
"Engineering best practices"), so a sibling module holds the new surface
instead of forcing a decomposition mid-sprint. This module later crossed
that same cap itself once the hybrid capacity preflight (item 2 below) grew
large enough to need its own tests and types; `_capacity_eval.py` now holds
that verdict layer. It imports this module's sizing primitives by module-
object reference (see its own docstring for why) rather than the other way
around -- this module imports NOTHING from `_capacity_eval`, so the
dependency stays strictly one-way and there is no import cycle.

TWO THINGS LIVE HERE, ONE ROOT CAUSE EACH FIXES:

1. PHASE-AWARE CAPS (`memory_limit_for` / `resolve_phase_memory_limits`).
   `resolve_ooc_memory_limit` (`_budget.py`) resolves ONE `memory_limit`
   string, divided by the run's GLOBAL WORST-CASE concurrency, and
   `_runner.py` used to thread that single string into every DuckDB
   connection it opens. But DuckDB instances on this route open and close
   in PHASES with different LOCAL liveness: a table's incoming-edge joiners
   are co-live with each other, but the SINK path always closes them
   (`_emit.py`'s `on_stream_consumed`) before that table's own outgoing
   relation build opens -- so the build is the ONLY live instance then, not
   two. Dividing every phase by the global peak starves any phase whose
   LOCAL live count is below that peak; a measured 100M-row/4GB cloud run
   OOMed with half its budget idle for exactly this reason. `memory_limit_
   for` is the single place a byte budget becomes one phase's per-connection
   cap; `resolve_phase_memory_limits` derives the caps `_runner.py` needs
   for one table (joiner / sink-path build / resident-path build) from its
   own incoming-edge count, plain counts from its caller like `_budget.py`
   rather than depending on `decoy_engine.relationships`.

2. THE BUILD-FLOOR MODEL (`predict_ooc_build_floor_bytes`) AND ITS CEILING
   INVERSE (`declared_minimum_ceiling_bytes`), which the capacity verdict
   layer (`_capacity_eval.evaluate_capacity` / `enforce_ooc_memory_
   preflight`) prices jobs against. Phase-aware caps LOWER the floor but
   cannot always eliminate it: a big enough parent table's relation-build
   dedup still needs real non-spillable resident state that DuckDB cannot
   push to `temp_directory` no matter how generous the `memory_limit`.
   ROUND-4: the build-floor gate is now ADVISORY, not hard-fail -- the
   measured completion caps showed the old flat refusal over-rejected jobs
   that would have completed, so a floor that exceeds the build's cap now
   returns FIT with a warning and a recommended host size instead of
   refusing the job. The one remaining HARD refusal in this preflight is
   fan-in (co-live DuckDB instances that cannot each fit even a 1 MB
   `memory_limit` under the budget); see `_capacity_eval.py`'s module and
   `CapacityVerdict` docstrings for the current gate order. The gate's own
   mechanics (thresholds, the per-table loop, warn fractions) live in
   `_capacity_eval.py`; this module keeps only the row-based floor model and
   the ceiling math that gate calls.

   `predict_ooc_build_floor_bytes` MUST BE COMPARED AGAINST THE EXACT CAP
   THE BUILD WILL GET, NOT A FRACTION OF THE RAW MEMORY CEILING.
   `resolve_ooc_memory_limit` (`_budget.py`) already subtracts a reserve
   from the ceiling, and item 1's phase-aware division then splits THAT
   budget again by per-table liveness; a caller re-deriving its own
   fraction of the raw ceiling can pass a job whose real, phase-aware cap is
   starved -- the BLOCKER this sprint's remediation closes (a 100M-row/4GB
   run was ADMITTED by a ceiling-fraction check, then OOMed inside DuckDB's
   own accounting). `_capacity_eval.evaluate_capacity` computes `cap(t)` via
   `actual_duckdb_cap_bytes(budget_bytes, live)`, the SAME per-instance
   computation `memory_limit_for` uses to size the real connection's
   string. `budget_bytes // live` ALONE is NOT `cap(t)`: DuckDB reads
   `memory_limit_for`'s `"NNMB"` string as base-10 megabytes, so the true
   cap is `actual_duckdb_cap_bytes`'s smaller decimal number -- comparing a
   floor to the larger binary number let a job whose floor cleared it but
   exceeded the real decimal cap through, then OOM inside DuckDB (round-2's
   remediation closes this denomination mismatch, on top of item 1's
   phase-liveness one). `declared_minimum_ceiling_bytes` inverts that same
   actual-cap model to report a truthful minimum ceiling for the build-floor
   advisory, never a raw-ceiling approximation -- see item 2 above for why
   this preflight's build-floor check is advisory (warn + recommend) like
   the sibling `_spill_estimate.enforce_ooc_disk_preflight`, while fan-in
   stays the one HARD refusal: a co-live DuckDB instance that cannot even
   fit the 1 MB minimum `memory_limit` has no runtime backstop, unlike a
   build floor that measurement showed often completes past its predicted
   cap, or a disk spill that has its own soft failure mode.
"""

from __future__ import annotations

import math

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._budget import (
    _OOC_RESERVE_FLOOR_BYTES,
    _OOC_RESERVE_FRACTION,
)

__all__ = [
    "actual_duckdb_cap_bytes",
    "declared_minimum_ceiling_bytes",
    "memory_limit_for",
    "predict_ooc_build_floor_bytes",
    "resolve_phase_memory_limits",
]


def _per_instance_mib(budget_bytes: int, live_instances: int) -> int:
    """The whole-MiB size one live instance's cap gets when `budget_bytes` is
    split `live_instances` ways -- the SINGLE computation `memory_limit_for`
    and `actual_duckdb_cap_bytes` both derive from, so the two never drift
    apart.

    DECIMAL-correct (round-3 Fix C, closes round-2's over-correction):
    `memory_limit_for` emits a decimal `"NNMB"` string, so refusing on the
    BINARY split alone (round-2) wrongly refused splits that still fit
    DuckDB's smaller decimal 1 MB floor -- floor in binary MiB first,
    escalate to the decimal question only when that floor is 0.

    INVARIANT PROOF -- `live_instances * actual_duckdb_cap_bytes(...) <=
    budget_bytes` always: for `n = per_instance_mib >= 1`, `live * n * 1e6 <=
    live * (budget/(live*1_048_576)) * 1e6 = budget * 0.95367 < budget`; for
    `n == 0 -> return 1`, that fires only when `live * 1e6 <= budget_bytes`.
    Only a genuine over-commit (even DuckDB's 1 MB floor cannot fit every
    live instance) raises. Codex acceptance: `(64 MiB, 67 live)` admits
    ("1MB", `67e6 <= 67_108_864`); `(64 MiB, 68 live)` raises (`68e6 >
    67_108_864`).
    """
    if live_instances < 1:
        raise ExecutionError(
            code="out_of_core_concurrency_invalid",
            message=f"live_instances must be >= 1, got {live_instances}.",
        )
    per_instance_mib = (budget_bytes // live_instances) // (1024 * 1024)
    if per_instance_mib < 1:
        # Floors below 1 binary MiB, so the emitted cap is DuckDB's minimum
        # "1MB" (1_000_000 decimal bytes) -- safe iff the summed 1-MB caps
        # still fit the budget; only a genuine over-commit is refused.
        if live_instances * 1_000_000 > budget_bytes:
            raise ExecutionError(
                code="out_of_core_fanin_exceeds_budget",
                message=(
                    f"{live_instances} co-live DuckDB instances over a {budget_bytes}-byte "
                    "budget cannot each hold DuckDB's 1 MB minimum memory_limit without the "
                    "summed caps exceeding the budget; reduce fan-in or increase the budget."
                ),
            )
        return 1
    return per_instance_mib


def memory_limit_for(budget_bytes: int, live_instances: int) -> str:
    """One DuckDB connection's `memory_limit` for a phase where
    `live_instances` connections are co-live, sized so the SUM of every live
    instance's ACTUAL enforced cap never exceeds `budget_bytes` -- see
    `_per_instance_mib` for the shared sizing computation and the
    fail-closed guard that makes this invariant TRUE. Same strict-floor
    division `resolve_ooc_memory_limit` (`_budget.py`) already established,
    exposed as a free function so a caller with phase-local liveness (not
    the run's single global-peak concurrency number) can size each
    connection at the point it opens.
    """
    per_instance_mib = _per_instance_mib(budget_bytes, live_instances)
    # DuckDB reads "MB" as base-10, so the effective limit lands slightly
    # below the MiB byte count -- the safe direction, never over the cap.
    return f"{per_instance_mib}MB"


def actual_duckdb_cap_bytes(budget_bytes: int, live_instances: int) -> int:
    """The bytes DuckDB actually enforces for one connection sized by
    `memory_limit_for` -- its emitted decimal "MB" string re-read as bytes
    (`per_instance_mib * 1_000_000`), NOT the binary `budget_bytes //
    live_instances` a caller might naively compare a floor against.

    This is the number round-2 Fix B's preflight and declared-minimum gate
    against: DuckDB parses `"NNMB"` as base-10 megabytes, so the true cap is
    always slightly BELOW the binary MiB byte count that number suggests.
    Comparing a floor to the raw binary `budget_bytes // live_instances`
    instead admits a job whose floor cleared that larger binary number but
    exceeded this smaller true one -- the exact denomination mismatch Codex's
    round-2 gate reproduced. Shares `_per_instance_mib`'s fail-closed guard,
    so an un-sizeable (sub-1-MiB) split never returns a phantom cap here.
    """
    per_instance_mib = _per_instance_mib(budget_bytes, live_instances)
    return per_instance_mib * 1_000_000


def resolve_phase_memory_limits(
    *,
    budget_bytes: int | None,
    memory_limit: str | None,
    incoming_edges: int,
    sink: bool,
) -> tuple[str | None, str | None, str | None, str | None]:
    """`(sink_joiner, resident_joiner, sink_build, resident_build)`
    memory_limit strings for one table's incoming-edge joiners and its own
    outgoing relation build.

    `budget_bytes` is the UNDIVIDED per-run budget (`OutOfCoreBudget.
    budget_bytes`, threaded through as `run_fk_out_of_core`'s own
    `budget_bytes` param). `None` means host-RAM detection failed and no
    explicit budget was given -- every phase then falls back to the flat
    `memory_limit` string rather than inventing a budget to divide.

    `sink` (round-3 Fix C, SUB-FIX 2): only the opened path is computed (and
    can raise); the unopened pair falls back to flat `memory_limit`, like
    `budget_bytes is None` -- computing all four unconditionally (pre-fix)
    raised for a path never opened, a false refusal. Tuple SHAPE unchanged.

    Phase-local liveness differs by path, so the JOINER cap must too: a
    connection's `memory_limit` is fixed at open, and a joiner staying live
    into the build phase needs the SAME cap the build gets, not its own
    (narrower) phase. SINK: joiners are co-live with each other only (cap =
    budget // incoming_edges) and close before the build opens (`_emit.py`'s
    `on_stream_consumed`), so the build is the run's ONLY live instance then
    (cap = budget, undivided -- the fix for the measured 100M/4GB OOM).
    RESIDENT: joiners stay open THROUGH the build, so both share cap =
    budget // (incoming_edges + 1) -- one flat `joiner` value on both paths
    (pre-fix) let resident joiners open at the sink-sized cap while co-live
    with a build sized for the extra slot, so their SUM exceeded
    `budget_bytes`; this split fixes that (co-live sum is now
    `(incoming_edges + 1) * (budget // (incoming_edges + 1)) <=
    budget_bytes`). Zero incoming edges means no joiner opens, so the
    active path's joiner value goes unused.
    """
    if budget_bytes is None:
        return memory_limit, memory_limit, memory_limit, memory_limit
    if sink:
        sink_joiner = (
            memory_limit_for(budget_bytes, incoming_edges) if incoming_edges else memory_limit
        )
        sink_build = memory_limit_for(budget_bytes, 1)
        return sink_joiner, memory_limit, sink_build, memory_limit
    resident_joiner = (
        memory_limit_for(budget_bytes, incoming_edges + 1) if incoming_edges else memory_limit
    )
    resident_build = memory_limit_for(budget_bytes, incoming_edges + 1)
    return memory_limit, resident_joiner, memory_limit, resident_build


# --- Part B: the build-floor model and its ceiling inverse ------------------

# The relation-build dedup's non-spillable floor, per row of the largest
# parent table (see `predict_ooc_build_floor_bytes`'s docstring for how this
# is used). ROUND-4 RECALIBRATION (this constant): the mandatory measurement
# (`build_floor_probe`) established that every build entrypoint -- resident,
# sink-streamed, all of them -- funnels through the ONE `_relation.py::
# _build_relation`, and that relation build is ROW-LINEAR on every path. It
# is NOT the pre-Phase-4 `arg_max` O(distinct-key) RESIDENT-blowup operator
# the 190 B/row constant this replaces was calibrated against; that operator
# was REMOVED when the split-dedup landed, and the 33.3M-row cloud OOM it
# produced is retired as HISTORICAL context, not a binding anchor for the
# CURRENT model (see below for why the cloud point still informs the slope,
# just not as "the same operator, uncorrected").
#
# UNITS: `floor_bytes` is measured and compared in DuckDB `memory_limit`
# COMPLETION-CAP bytes -- the smallest cap a build COMPLETES under, per
# `build_floor_probe` -- NOT peak RSS. This is the number `evaluate_capacity`
# compares against `actual_duckdb_cap_bytes`, and the number
# `declared_minimum_ceiling_bytes` inverts through the reserve model to a
# host recommendation; feeding a peak-RSS figure into either would compare
# two different quantities against the same threshold. VmHWM (peak RSS) is
# checked SEPARATELY, as a "does the recommended host cover observed peak
# residency" sanity check, never folded into `floor_bytes` itself.
#
# MEASURED COMPLETION CAPS (`build_floor_probe`, this devbox, int64-ish
# keys, DuckDB's non-monotonic memory_limit behavior bracketed by confirming
# a STABLE pass, not just one lucky tier):
#   5M rows  -> completes at <= 256 MiB
#   10M rows -> completes at <= 256 MiB
#   20M rows -> completes at ~768 MiB (fails at 512 MiB)
# These three points alone imply a per-row need far below the old 190 B/row
# -- the 20M point is the tightest devbox anchor and sits at roughly
# (768 MiB - 256 MiB) / 15M rows =~ 36 B/row above the 5M/10M floor, i.e. tens
# of bytes per row, not hundreds.
#
# CROSS-ENVIRONMENT ANCHOR: the GCP n2-standard-8 33.3M-row/table run
# (`fk_memory_probe out_of_core`) completed only at a ~3.3 GB per-instance
# memory_limit (it OOMed near 1 GB). Read as a completion-cap point rather
# than the old resident-blowup framing, that implies up to ~98 B/row once
# cross-environment allocator fragmentation and the real route's wider
# staged payloads (streamed sink, orphan handling) are accounted for -- a
# real, still-row-linear cost the isolated devbox sweep alone would not
# surface at smaller row counts.
#
# **PINNED: 120 B/row.** This clears the GCP cloud completion need
# (~98 B/row) with margin while sitting far below the retired 190 B/row
# slope that was fit to the old, removed `arg_max` operator and so
# systematically over-recommended host memory for every job it priced. The
# claim this constant supports is "conservative over the MEASURED completion
# domain, at typical (int64-ish) key widths" -- NOT an unconditional "never
# under-predicts any requirement at any key width"; a materially wider
# composite key would need its own measured point before this slope could be
# trusted for it (tracked as follow-on measurement work, not blocking this
# recalibration -- see the plan's acceptance tests for the domain this
# constant is asserted over).
_BUILD_FLOOR_BYTES_PER_ROW = 120.0

# BASE is the fixed DuckDB relation-build overhead at ~zero rows. It is kept
# SMALL for one structural reason: the preflight gates `floor(t)` against the
# ACTUAL DuckDB decimal cap (`actual_duckdb_cap_bytes`), and the smallest real
# cap is `_MIN_BUDGET_BYTES` (64 MiB) divided by phase liveness: on a resident
# fan-in-1 build under the route's 64 MiB byte-estimate routing knob that is
# `(64 MiB // 2) // 1 MiB * 1_000_000 = 32_000_000 B`. A base large enough to
# push `floor(40 rows)` past that cap would make a genuinely tiny job's floor
# exceed the knob and lose byte-transparency, a hard requirement (40-row
# fixtures, `tests/parity/test_out_of_core_*_routing.py`).
#
# Set to 28 MiB by the round-4 recalibration, up from 24 MiB. The 24 MiB base
# paired with the recalibrated 120 B/row slope put `floor(100k) = 37_165_824 B`
# BELOW the reproduced 40_000_000 B FAILING tier at 100k rows -- i.e. it
# under-predicted a memory_limit KNOWN to OOM, breaking the "conservative over
# every measured failing tier" property. The small end is base-dominated, so
# the base (not the slope) is the right lever there: 28 MiB
# (29_360_128 B) makes `floor(100k) = 29_360_128 + 120*100_000 = 41_360_128 B`,
# which sits inside the measured (40_000_000, 44_000_000] bracket (above the
# fail edge, at or below the pass edge) -- a proper bracket, not an
# under-prediction. It stays byte-transparent: `floor(40 rows) = 29_360_128 +
# 120*40 = 29_364_928 B`, ~2.6 MB under the 32_000_000 B routing-knob cap. The
# large end barely moves (the base is negligible against 120*20M), so the
# recalibration's loosening is preserved. A near-zero parent still opens one
# real DuckDB instance, so the floor never predicts near zero.
_BUILD_FLOOR_BASE_BYTES = 28 * 1024 * 1024


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
    under-prediction understates the recommended host size for a job that
    then OOMs mid-run, which the advisory exists to warn about before it
    happens; an over-prediction only recommends more memory than the job
    strictly needed. Round-4: this bias no longer costs a wrongly-refused
    job either way -- the build-floor gate is advisory (FIT + `warned`), so
    an over-prediction cannot refuse a job that would have completed; only
    fan-in still hard-refuses (`_capacity_eval.py`). `max(0, max_parent_rows)`
    guards a caller-supplied negative row count from producing a smaller
    (wrong-direction) floor.
    """
    rows = max(0, max_parent_rows)
    return _BUILD_FLOOR_BASE_BYTES + int(_BUILD_FLOOR_BYTES_PER_ROW * rows)


def declared_minimum_ceiling_bytes(floor_bytes: int, *, incoming_edges: int, sink: bool) -> int:
    """The smallest whole-GiB memory ceiling that, fed back through
    `resolve_ooc_memory_limit` then `actual_duckdb_cap_bytes`, yields a build
    cap >= `floor_bytes` -- the truthful "you need approximately N GB" the
    build-floor advisory reports (round-4: this is now always a warning,
    never a refusal -- see `_capacity_eval.py`).

    Round-2 Fix B: this must target the ACTUAL decimal DuckDB cap, not the
    binary `budget // live` round 1 inverted. `actual_duckdb_cap_bytes(budget,
    live) >= floor_bytes` requires `per_instance_mib >=
    ceil(floor_bytes / 1_000_000)`, hence `budget // live >= per_instance_mib
    * 1024 * 1024`. `required_budget` below adds one extra `1024*1024*live` of
    slack on top of that tight bound, to absorb the double floor-division
    (`budget // live`, then `// 1024**2`) `_per_instance_mib` performs -- so
    this function's own rounding never straddles the real boundary. Verified
    by round-trip test (`TestDeclaredMinimumCeiling`): the returned ceiling,
    fed back through `resolve_ooc_memory_limit` then `actual_duckdb_cap_bytes`,
    empirically clears `floor_bytes` at every tested (rows, incoming, sink).

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

    `live` is `1` on the sink path (the build gets the undivided budget) but
    `incoming_edges + 1` on the resident path: `resolve_phase_memory_limits`
    divides the resident build's own connection by that same divisor, so a
    ceiling that only clears the UNDIVIDED floor would still starve the build
    once resolved back through the real phase-aware cap.
    """
    live = 1 if sink else incoming_edges + 1
    target_mib = -(-floor_bytes // 1_000_000)  # ceil(floor_bytes / 1e6)
    # +1 MiB of slack per live instance to absorb the double floor-division
    # (`budget // live`, then `// 1024**2`) `_per_instance_mib` performs --
    # see the docstring above for why this is the safe (never-under) side.
    required_budget = (target_mib + 1) * 1024 * 1024 * live
    small_regime_ceiling = required_budget + _OOC_RESERVE_FLOOR_BYTES
    if _OOC_RESERVE_FRACTION * small_regime_ceiling <= _OOC_RESERVE_FLOOR_BYTES:
        ceiling_needed_bytes = small_regime_ceiling
    else:
        ceiling_needed_bytes = math.ceil(required_budget / (1 - _OOC_RESERVE_FRACTION))
    gib = 1024**3
    whole_gib = -(-ceiling_needed_bytes // gib)  # ceil to a whole GiB, never under
    return whole_gib * gib

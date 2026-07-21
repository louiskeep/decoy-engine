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

2. THE HYBRID CAPACITY PREFLIGHT (`predict_ooc_build_floor_bytes` /
   `enforce_ooc_memory_preflight`). Phase-aware caps LOWER the floor but
   cannot always eliminate it: a big enough parent table's relation-build
   dedup still needs real non-spillable resident state that DuckDB cannot
   push to `temp_directory` no matter how generous the `memory_limit`.
   Cam's governing decision is a HYBRID gate: warn near that floor,
   hard-fail beyond it, so a job that cannot fit is refused BEFORE any
   DuckDB work runs rather than left to OOM mid-job -- the never-crash
   guarantee, with item 1 above as the floor-lowering fix underneath it.

   THE GATE MUST COMPARE AGAINST THE EXACT CAP THE BUILD WILL GET, NOT A
   FRACTION OF THE RAW MEMORY CEILING. `resolve_ooc_memory_limit` (`_budget.
   py`) already subtracts a reserve from the ceiling, and item 1's
   phase-aware division then splits THAT budget again by per-table
   liveness; a preflight re-deriving its own fraction of the raw ceiling can
   pass a job whose real, phase-aware cap is starved -- the BLOCKER this
   sprint's remediation closes (a 100M-row/4GB run was ADMITTED by a
   ceiling-fraction check, then OOMed inside DuckDB's own accounting). So
   `enforce_ooc_memory_preflight` takes `budget_bytes` -- THE SAME
   `OutOfCoreBudget.budget_bytes` `run_out_of_core_route` resolves at its one
   `resolve_ooc_memory_limit` call site, reused rather than re-derived --
   plus sink-ness and each table's own incoming-edge count, and computes
   `cap(t)` via `actual_duckdb_cap_bytes(budget_bytes, live)`, the SAME
   per-instance computation `memory_limit_for` uses to size the real
   connection's string. `budget_bytes // live` ALONE is NOT `cap(t)`:
   DuckDB reads `memory_limit_for`'s `"NNMB"` string as base-10 megabytes,
   so the true cap is `actual_duckdb_cap_bytes`'s smaller decimal number --
   comparing a floor to the larger binary number let a job whose floor
   cleared it but exceeded the real decimal cap through, then OOM inside
   DuckDB (round-2's remediation closes this denomination mismatch, on top
   of item 1's phase-liveness one). The warn/fail fractions below multiply
   THAT actual cap, never the raw ceiling nor the binary division -- see
   `enforce_ooc_memory_preflight`'s docstring for why this preflight is a
   HARD gate where the sibling `_spill_estimate.enforce_ooc_disk_preflight`
   is advisory-only (disk spill has a soft failure mode; a resident-memory
   floor above the cap does not).
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
    "actual_duckdb_cap_bytes",
    "declared_minimum_ceiling_bytes",
    "enforce_ooc_memory_preflight",
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
    active path's joiner value goes unused. (OOC-B / fix#1: the joiner cap now
    bounds a SPILLABLE streamed join, not a pinned parent TEMP TABLE.)
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
# UNIT NOTE (round-2 remediation): both `scripts/build_floor_probe.py` and
# `memory_limit_for` hand DuckDB a decimal `"NNMB"` string, which DuckDB reads
# as base-10 megabytes (`NN * 1_000_000` bytes), NOT `NN * 1024 * 1024`
# (mebibytes). Every bracket below is therefore in DECIMAL-byte tiers -- an
# `--memory-limit-mib 44` probe run is a 44_000_000-byte cap, not a
# 46_137_344-byte one. Round 1's calibration comment labeled these tiers
# "MiB" and treated "above the highest tested failure" as the safe threshold;
# that conflated the two units and left the 100k floor BELOW the true decimal
# pass edge (a BLOCKER: `floor(100k)` at slope 110 was 36_165_824 B, under the
# real 44_000_000 B pass edge). This comment and the slope below are stated
# in actual bytes throughout to close that gap.
#
# Dataset 1 -- CLEAN isolated devbox sweep (`scripts/build_floor_probe.py`,
# `build_parent_key_relation` run directly on int64-keyed parent.parquet,
# RLIMIT_DATA FIXED at 8000 MiB across EVERY tier so a failure is
# attributable to `memory_limit` alone, never the rlimit). This removes the
# prior calibration's confound: its 2048 MiB-FAIL anchor used RLIMIT_DATA
# 3000 MiB and bad_alloc'd at peak_total_mem 1329 MiB -- BELOW its own cap,
# an rlimit/fragmentation artifact, not a memory_limit floor. The clean
# per-row-count floor bracket (highest FAIL, lowest PASS] in DECIMAL BYTES,
# scanned across a WIDE tier range because DuckDB's memory_limit does not
# always fail monotonically -- a larger limit can pick a worse partition
# count and fail where a smaller limit passed, e.g. 1M below):
#   100k rows -> (40e6, 44e6]    20M rows -> (256e6, 384e6]
#   1M rows   -> (96e6, 128e6]   40M rows -> FAILS at 512e6 (floor > 512e6 B;
#   5M rows   -> (64e6, 80e6]                the pass edge was not cleanly narrowed)
#   10M rows  -> (128e6, 192e6]
# The 100k bracket is RE-REPRODUCED at this commit (round-2 remediation,
# fixed RLIMIT_DATA 8000 MiB): 34/35/36/37/40 MB -> fail, 44/48 MB -> pass,
# confirming (40e6, 44e6] as the true floor. The other row counts' brackets
# are carried forward from the round-1 sweep (not re-run this round -- see
# the module's report-back for what WAS re-verified). The clean isolated
# floor is roughly linear at only ~1.3-1.9 bytes per row.
#
# Dataset 2 -- REAL-ROUTE cloud measurement (fk_memory_probe out_of_core, GCP
# n2-standard-8, 33.3M-row parent per table). The 33.3M-row BUILD phase OOMed
# at memory_limit ~1638 MB and COMPLETED at 2457 MB (decimal, same unit note
# above): floor(33.3M) in (~1638e6, 2457e6]. This is ~4x higher per-row than
# the isolated devbox sweep predicts, because the isolated probe strips away
# everything the real route's build coexists with (the streamed sink
# pipeline, orphan handling, wider staged payloads, cross-environment
# allocator fragmentation). The 33.3M/4GB real-route OOM is THE motivating
# failure this whole preflight exists to refuse; it is a valid observed floor
# (a real memory_limit OOM, NOT an rlimit artifact), so "never under-predict
# any observed floor" FORCES the model to clear it. The isolated sweep is
# therefore a LOWER witness (the model must over-predict it, which it does by
# ~4-10x); the cloud point is the BINDING upper anchor.
#
# FIT: base + slope, in actual (decimal) bytes throughout. SLOPE must clear
# TWO binding points at once: the cloud passing edge (2457e6 B at 33.3M rows)
# AND the reproduced 100k decimal-byte floor (44e6 B) -- round 1's slope (110
# B/row) cleared the former but not the latter (a slope fit only to the cloud
# anchor is too shallow to lift the SMALL-row chord above its own, much
# tighter per-row, decimal pass edge). **PINNED: 190 B/row.** `floor(100k) =
# 24*1024*1024 + 190*100_000 = 44_165_824 B`, clearing the 44_000_000 B pass
# edge by 165_824 B (tight by construction: the chord is fit to this exact
# point). The never-OOM guarantee still rests on the SLOPE where real OOMs
# occur, and 190 B/row over-predicts every other anchor with WIDER margin as
# rows grow: floor(1M) ~= 215 MB >= 128e6; floor(5M) ~= 975 MB >= 80e6;
# floor(10M) ~= 1.925 GB >= 192e6; floor(20M) ~= 3.825 GB >= 384e6; floor(40M)
# ~= 7.625 GB > 512e6; floor(33.3M) ~= 6.35 GB >= 2457e6 (a ~2.6x envelope
# over the cloud completion level, heavier over-refusal than round 1's ~1.5x
# -- the SAFE direction per the never-under-predict rule; a DOCUMENTED KNOWN
# carry-forward, not a new blocker).
_BUILD_FLOOR_BYTES_PER_ROW = 190.0

# BASE is the fixed DuckDB relation-build overhead at ~zero rows, and unlike
# the slope it does NOT carry the never-OOM guarantee (real OOMs are a
# large-row, slope-dominated regime). It is deliberately SMALL -- 24 MiB
# (25_165_824 B), below the reproduced 40_000_000 B highest-FAIL tier at 100k
# rows -- for one structural reason: the preflight gates `floor(t)` against
# the ACTUAL DuckDB decimal cap (`actual_duckdb_cap_bytes`, round-2's Fix B),
# and the smallest real cap is `_MIN_BUDGET_BYTES` (64 MiB) divided by phase
# liveness: on a resident fan-in-1 build under the route's 64 MiB
# byte-estimate routing knob that is `(64 MiB // 2) // 1 MiB * 1_000_000 =
# 32_000_000 B`. A larger base would refuse a genuinely tiny job (tens of
# rows) whose real floor is a few MB -- and byte-transparency for those jobs
# is a hard requirement (`tests/parity/test_out_of_core_*_routing.py`, 40-row
# fixtures under the 64 MiB knob). UNCHANGED at 24 MiB by round-2's remediation
# (only the slope moved): `floor(40 rows) = 25_165_824 + 190*40 =
# 25_173_424 B`, still ~6.8 MB under the 32_000_000 B routing-knob cap, while
# the SLOPE alone lifts `floor(100k) = 44_165_824 B` above 100k's own
# 44_000_000 B pass edge (see `_BUILD_FLOOR_BYTES_PER_ROW`'s comment). A
# near-zero parent still opens one real DuckDB instance, so the floor never
# predicts near zero.
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

    SCOPE (OOC-B / fix#1): prices the relation-BUILD phase only (`_relation.py`,
    untouched by fix#1, so UNCHANGED), never the now-spillable FK-joiner phase.
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
    `resolve_ooc_memory_limit` then `actual_duckdb_cap_bytes`, yields a build
    cap >= `floor_bytes` -- the truthful "you need approximately N GB" a
    hard-fail or warning reports.

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

    Deliberately asymmetric with the sibling disk preflight (advisory-only,
    `_spill_estimate.enforce_ooc_disk_preflight`): an under-predicted disk
    estimate still hits the runtime `check_temp_disk_budget` backstop and
    aborts cleanly, but a resident-memory floor above its cap has no such
    backstop -- DuckDB's allocator raises a raw, uncatchable "bad
    allocation" mid-query -- so THIS preflight must actually reject.

    `parent_table_rows` prices one row count per build-phase (outgoing-FK)
    table; `budget_bytes` is the SAME `OutOfCoreBudget.budget_bytes`
    `resolve_ooc_memory_limit` resolved at the route's one call site
    (reused, never re-derived); `sink` is whether this run streams to a
    sink; `incoming_edge_counts` is each table's own fan-in. For every
    table `t`:

        live(t)  = 1                                      if sink
                 = incoming_edge_counts[t] + 1              otherwise
        floor(t) = predict_ooc_build_floor_bytes(parent_table_rows[t])
        cap(t)   = actual_duckdb_cap_bytes(budget_bytes, live(t))

    the SAME split `memory_limit_for` uses to size the real connection AND
    the ACTUAL bytes DuckDB enforces (round-2 Fix B: the binary
    `budget_bytes // live(t)` alone is a larger number that would admit a
    floor exceeding the true decimal cap). `cap(t)`'s own sizing can raise
    `out_of_core_fanin_exceeds_budget` (round-2 Fix C) on an un-sizeable
    split, before any DuckDB work, same as an insufficient-memory hard-fail.
    Otherwise HARD-FAILS (`out_of_core_insufficient_memory`, before any
    DuckDB work) if `floor(t) > cap(t)` for ANY `t` (binding table =
    `argmax_t (floor(t) - cap(t))`); otherwise WARNS (never blocks) if
    `floor(t) >= _OOC_MEM_WARN_FRACTION * cap(t)` for the tightest such
    table. `budget_bytes=None` (host-RAM detection failed, no explicit
    budget, mirroring `resolve_ooc_memory_limit`) fails OPEN: Part A's caps
    fall back to the flat `memory_limit` here too, so no real per-table cap
    is left to gate a floor against.

    A pure-joiner leaf (incoming edges only, no build) has no `floor(t)`,
    so it is not in `parent_table_rows`; its OWN fan-in is still guarded
    HERE, up front (round-3 Fix C, SUB-FIX 3): after the build-phase loop,
    every table in `incoming_edge_counts` (leaves included) has its
    joiner's `actual_duckdb_cap_bytes` called to trigger the shared guard,
    at the JOINER split -- a DIFFERENT number than build-phase `live(t)`
    above on the sink path (`1`), so this is a distinct check.
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
        live = 1 if sink else incoming + 1
        # Raises out_of_core_fanin_exceeds_budget (round-2 Fix C) before any
        # DuckDB work if this table's own split is un-sizeable -- the
        # earliest point this preflight has the information to catch it.
        cap_bytes = actual_duckdb_cap_bytes(budget_bytes, live)
        margin = floor_bytes - cap_bytes
        if worst_fail is None or margin > worst_fail[0]:
            worst_fail = (margin, table, floor_bytes, cap_bytes)
        if floor_bytes >= _OOC_MEM_WARN_FRACTION * cap_bytes:
            if worst_warn is None or margin > worst_warn[0]:
                worst_warn = (margin, table, floor_bytes, cap_bytes)

    # SUB-FIX 3: guard every joiner's fan-in up front too (leaves included).
    # `live` is the JOINER split, differing from build-phase `live` above on
    # the sink path (`incoming` vs `1`); return value unused, just triggers.
    for incoming in incoming_edge_counts.values():
        live = incoming if sink else incoming + 1
        actual_duckdb_cap_bytes(budget_bytes, live)

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

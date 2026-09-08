"""The out-of-core FK route's capacity verdict layer: the row-based build-
floor model in `_memory_estimate.py` fed through the hybrid warn/hard-fail
gate (`evaluate_capacity` / `enforce_ooc_memory_preflight`), shared by the
mid-run preflight and the CLI's estimate-only `estimate_job_capacity` path.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _memory_estimate as _mem

_logger = logging.getLogger(__name__)

__all__ = [
    "CapacityEstimate",
    "CapacityInputs",
    "CapacityVerdict",
    "MemoryPreflight",
    "enforce_ooc_memory_preflight",
    "evaluate_capacity",
]

# The one route name `evaluate_capacity` treats as "this job's memory floor
# is actually priceable" -- anything else (full_frame, sequential, a
# rejected-before-read job, "no relationships at all") is NOT_APPLICABLE by
# construction, matching the OOC-FK-route-only scope the CLI capacity checker
# documents (v1 does not cover generate-path or non-FK single-table sizing).
_OUT_OF_CORE_ROUTE = "out_of_core"

# The one refusal code `evaluate_capacity`'s fan-in guard (via
# `actual_duckdb_cap_bytes` -> `_per_instance_mib`) can raise -- caught
# narrowly so it converts to an INSUFFICIENT verdict instead of propagating,
# while any OTHER `ExecutionError` (e.g. `out_of_core_concurrency_invalid`, a
# caller-usage bug, not a capacity refusal) still propagates untouched (R3:
# an unexpected estimator exception is never swallowed into a verdict).
_FANIN_EXCEEDS_BUDGET_CODE = "out_of_core_fanin_exceeds_budget"

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


class CapacityVerdict(str, Enum):
    """The tri-plus-one state `evaluate_capacity` returns.

    ROUND-4 CONTRACT CHANGE: `FIT` no longer means "clears the build-floor
    budget" -- it means "no hard impossibility detected". The build-floor
    prediction (`predict_ooc_build_floor_bytes`) is now advisory only: a
    table whose predicted floor exceeds its build cap still returns `FIT`,
    with `warned=True` and a recommended host size, never `INSUFFICIENT`.
    `INSUFFICIENT` is reached ONLY through the fan-in guards (a co-live
    DuckDB instance split that cannot fit even a 1 MB `memory_limit` under
    the budget) -- the one impossibility this preflight still treats as
    hard, because unlike the build-floor prediction, which recommends a
    size that may or may not turn out to matter, an un-sizeable fan-in split
    cannot be provisioned around at all. Only `INSUFFICIENT` may ever cause
    a caller to refuse a job or exit a distinct capacity code -- `UNKNOWN`
    and `NOT_APPLICABLE` are both "no verdict", for different reasons, and
    neither is a refusal:

    - `FIT`: no hard impossibility detected. `warned=True` marks an adverse
      build-floor prediction -- a recommendation, not a refusal; the job is
      not blocked either way.
    - `INSUFFICIENT`: a fan-in split that cannot be sized at all -- `code`
      names the refusal (`out_of_core_fanin_exceeds_budget`).
    - `UNKNOWN`: EXPECTED indeterminacy -- the budget is undetectable, or a
      parent table's row count cannot be priced exactly (e.g. a CSV source,
      whose count is a byte-size estimate). Never treat this as a pass; also
      never treat it as a failure -- a genuinely unpriceable job is neither.
    - `NOT_APPLICABLE`: this job's execution route is not `out_of_core`, so
      the out-of-core-FK memory floor this evaluator prices does not apply
      to it at all (a different route, a rejected-before-read job, or a job
      with no FK relationships).
    """

    FIT = "fit"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CapacityInputs:
    """The typed, already-derived inputs `evaluate_capacity` prices.

    Deliberately the SAME shape `enforce_ooc_memory_preflight` always
    computed inline (`parent_table_rows`, `incoming_edge_counts`, `sink`)
    plus two fields only the estimate-only entrypoint ever populates:

    - `route`: the execution route this job's config/graph actually
      resolves to (`"out_of_core"`, `"full_frame"`, `"sequential"`, or a
      caller-chosen label for a rejected-before-read job). The mid-run gate
      always passes `"out_of_core"` (it only ever runs on that route);
      `estimate_job_capacity` passes whatever the routing decision returned,
      so `evaluate_capacity` can return `NOT_APPLICABLE` for any other route
      without the caller having to special-case it up front.
    - `unresolved_parent_tables`: parent tables whose row count could not be
      priced EXACTLY (a CSV byte-size estimate, not a footer/exact count).
      Always empty on the mid-run path (every source there is a real
      `pa.Table` or `LazySource`, always exact); populated only by
      `estimate_job_capacity` when a parent table's source format has no
      cheap exact count. Non-empty forces `UNKNOWN` -- never refuse on an
      approximation (R6).
    """

    route: str
    parent_table_rows: Mapping[str, int]
    incoming_edge_counts: Mapping[str, int]
    sink: bool
    unresolved_parent_tables: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class CapacityEstimate:
    """The verdict `evaluate_capacity` returns -- the ONE result shape both
    the mid-run gate and the estimate-only entrypoint agree on.

    `verdict`/`code`/`needed_bytes`/`available_bytes`/`route`/`message` are
    the public contract a CLI caller renders and asserts against (both
    refusal codes are carried in `code`, never only embedded in `message`
    text). `needed_bytes`/`available_bytes` are exact byte counts; GiB is a
    display-only conversion of them, never a separate source of truth.

    `warned`/`binding_table`/`floor_bytes`/`cap_bytes` exist so
    `enforce_ooc_memory_preflight` can reconstruct the pre-existing
    `MemoryPreflight` shape (its own warn-band advisory, which predates this
    evaluator and has its own tests) without re-running the floor/cap loop a
    second time -- a CLI caller normally has no reason to read them.
    """

    verdict: CapacityVerdict
    code: str | None
    needed_bytes: int | None
    available_bytes: int | None
    route: str
    message: str
    warned: bool = False
    binding_table: str | None = None
    floor_bytes: int = 0
    cap_bytes: int | None = None


def evaluate_capacity(inputs: CapacityInputs, budget_bytes: int | None) -> CapacityEstimate:
    """The PURE capacity evaluator both gates share (R1 anti-drift): given
    already-derived `CapacityInputs` and a resolved `budget_bytes` (the SAME
    `OutOfCoreBudget.budget_bytes` `resolve_ooc_memory_limit` returns, never
    re-derived by a caller), decide FIT/INSUFFICIENT/UNKNOWN/NOT_APPLICABLE
    without raising for the ordinary refusal case.

    This is the per-table floor/cap loop `enforce_ooc_memory_preflight` used
    to run inline, extracted so a second caller (`estimate_job_capacity`) can
    ask the SAME question the mid-run gate answers -- on the SAME typed
    inputs -- without duplicating the loop, the warn-band threshold, or the
    message wording. `enforce_ooc_memory_preflight` now calls this and raises
    only when the verdict is `INSUFFICIENT`; the estimate-only path just
    returns whatever this function returns.

    Route gate first: a job whose route is not `out_of_core` has nothing
    here to evaluate.

    ROUND-4 REORDER: fan-in is checked NEXT, before the unresolved-rows and
    budget-None "no verdict" returns, whenever a usable `budget_bytes`
    exists -- fan-in only needs `incoming_edge_counts` and the budget, not a
    priced row count, so it is INDEPENDENT of the reasons those two returns
    exist for. Checking it first means a job with both unpriceable rows AND
    a fan-in impossibility still reports `INSUFFICIENT` (a real, actionable
    refusal) instead of being masked by `UNKNOWN` (acceptance test 5b). With
    no usable budget at all there is nothing budget-relative to compute, so
    fan-in is skipped and the existing `unresolved_parent_tables` /
    `budget_bytes is None` returns apply as before (test 5d).

    An un-sizeable fan-in split (`actual_duckdb_cap_bytes` raising
    `out_of_core_fanin_exceeds_budget`) is the ONE `ExecutionError` this
    function catches and folds into `INSUFFICIENT` -- it is the one
    refusal this evaluator still treats as hard (a co-live joiner/build
    split that cannot fit even DuckDB's 1 MB minimum), not a defect. Any
    OTHER `ExecutionError` (e.g. `out_of_core_concurrency_invalid`, a caller
    passing a malformed `live_instances`) is a genuine usage bug and
    propagates unchanged -- R3's rule that an unexpected estimator exception
    is never swallowed into a verdict.

    The build-floor prediction (the per-table loop below) is ADVISORY only
    (round-4): a table whose floor exceeds its build cap no longer returns
    `INSUFFICIENT`, only `FIT` with `warned=True` and a recommended host
    size. See `CapacityVerdict`'s docstring for the full contract change.

    Sizing primitives (`actual_duckdb_cap_bytes`, `predict_ooc_build_floor_
    bytes`, `declared_minimum_ceiling_bytes`) are called through `_mem`, the
    imported `_memory_estimate` MODULE OBJECT, rather than as bare names:
    this keeps the dependency one-way (this module imports `_memory_estimate`,
    never the reverse, so there is no cycle) and preserves the existing test
    seam (`tests/unit/execution/test_capacity_evaluator.py` monkeypatches
    `_memory_estimate.actual_duckdb_cap_bytes` directly on that module object,
    and attribute access here sees the patch).
    """
    if inputs.route != _OUT_OF_CORE_ROUTE:
        return CapacityEstimate(
            verdict=CapacityVerdict.NOT_APPLICABLE,
            code=None,
            needed_bytes=None,
            available_bytes=budget_bytes,
            route=inputs.route,
            message=(
                f"this job's execution route is {inputs.route!r}, not out_of_core; "
                "the out-of-core-FK capacity check does not apply to it."
            ),
        )

    if budget_bytes is not None:
        # Fan-in guard, row-independent, so it runs even for tables never
        # priced below (a table with incoming edges only, no build of its own,
        # is never in `parent_table_rows`), moved ahead of the unresolved-rows
        # / budget-None returns -- see this function's own docstring for why.
        # Liveness MUST equal the runtime's `_max_concurrent_ooc_instances`
        # peak, or the boundary refuses a table the runtime seats: a pure leaf
        # (no outgoing edge, hence no relation build) is `incoming` co-live
        # joiners, NOT `incoming + 1` -- only a table that also builds a
        # relation adds its own instance. A table builds iff it has an outgoing
        # edge, i.e. this caller was asked to price it (`parent_table_rows`) or
        # could not price it exactly (`unresolved_parent_tables`).
        parents = set(inputs.parent_table_rows) | inputs.unresolved_parent_tables
        for table, incoming in inputs.incoming_edge_counts.items():
            has_build = int(table in parents)
            live = max(incoming, has_build) if inputs.sink else incoming + has_build
            try:
                _mem.actual_duckdb_cap_bytes(budget_bytes, live)
            except ExecutionError as exc:
                if exc.code != _FANIN_EXCEEDS_BUDGET_CODE:
                    raise
                return CapacityEstimate(
                    verdict=CapacityVerdict.INSUFFICIENT,
                    code=exc.code,
                    needed_bytes=None,
                    available_bytes=budget_bytes,
                    route=inputs.route,
                    message=exc.message,
                    binding_table=table,
                )

    if inputs.unresolved_parent_tables:
        names = ", ".join(sorted(inputs.unresolved_parent_tables))
        return CapacityEstimate(
            verdict=CapacityVerdict.UNKNOWN,
            code=None,
            needed_bytes=None,
            available_bytes=budget_bytes,
            route=inputs.route,
            message=(
                f"cannot price an exact row count for table(s) {names} (no cheap exact "
                "count available for that source format); refusing to gate on an "
                "approximation, so capacity is not checked for this job."
            ),
        )
    if budget_bytes is None:
        return CapacityEstimate(
            verdict=CapacityVerdict.UNKNOWN,
            code=None,
            needed_bytes=None,
            available_bytes=None,
            route=inputs.route,
            message=(
                "available memory is not detectable on this host (no cgroup limit, no "
                "readable host RAM figure); capacity is not checked (fail-open)."
            ),
        )

    worst_fail: tuple[int, str, int, int] | None = None  # (margin, table, floor, cap)
    worst_warn: tuple[int, str, int, int] | None = None
    for table, rows in inputs.parent_table_rows.items():
        floor_bytes = _mem.predict_ooc_build_floor_bytes(rows)
        incoming = inputs.incoming_edge_counts.get(table, 0)
        live = 1 if inputs.sink else incoming + 1
        try:
            # Redundant safety: the pure-joiner-leaf loop above already
            # covers every table's fan-in split, but the build-phase `live`
            # here can differ from the joiner split on the sink path (1 vs
            # incoming), so this catch stays as its own guard.
            cap_bytes = _mem.actual_duckdb_cap_bytes(budget_bytes, live)
        except ExecutionError as exc:
            if exc.code != _FANIN_EXCEEDS_BUDGET_CODE:
                raise
            return CapacityEstimate(
                verdict=CapacityVerdict.INSUFFICIENT,
                code=exc.code,
                needed_bytes=None,
                available_bytes=budget_bytes,
                route=inputs.route,
                message=exc.message,
                binding_table=table,
            )
        margin = floor_bytes - cap_bytes
        if worst_fail is None or margin > worst_fail[0]:
            worst_fail = (margin, table, floor_bytes, cap_bytes)
        if floor_bytes >= _OOC_MEM_WARN_FRACTION * cap_bytes:
            if worst_warn is None or margin > worst_warn[0]:
                worst_warn = (margin, table, floor_bytes, cap_bytes)

    if worst_fail is not None and worst_fail[0] > 0:
        # ROUND-4 DEMOTION: a build-floor prediction that exceeds the build
        # cap no longer refuses the job -- it returns FIT with `warned=True`
        # and the same recommended-size math the old hard-fail used, now
        # framed as advice rather than a refusal. `binding_table`/
        # `floor_bytes`/`cap_bytes` stay populated so a caller can still
        # report which table drove the recommendation.
        _, table, floor_bytes, cap_bytes = worst_fail
        incoming = inputs.incoming_edge_counts.get(table, 0)
        floor_gib = floor_bytes / (1024**3)
        cap_gib = cap_bytes / (1024**3)
        needed_bytes = _mem.declared_minimum_ceiling_bytes(
            floor_bytes, incoming_edges=incoming, sink=inputs.sink
        )
        needed_gib = needed_bytes / (1024**3)
        return CapacityEstimate(
            verdict=CapacityVerdict.FIT,
            code=None,
            needed_bytes=needed_bytes,
            available_bytes=budget_bytes,
            route=inputs.route,
            message=(
                f"predicted relation-build floor ~{floor_gib:.2f} GiB for table {table!r} "
                f"exceeds the build cap ~{cap_gib:.2f} GiB it would receive; recommend a "
                f"host/cgroup ceiling of >= {needed_gib:.0f} GB. This is an advisory "
                "(relation-build only; excludes resident inputs, accumulated outputs, and "
                "ingestion peak); the job is not refused."
            ),
            warned=True,
            binding_table=table,
            floor_bytes=floor_bytes,
            cap_bytes=cap_bytes,
        )

    if worst_fail is None:
        # No parent tables at all (an out-of-core job with no build-phase
        # table to price) -- nothing to warn about, nothing to recommend.
        return CapacityEstimate(
            verdict=CapacityVerdict.FIT,
            code=None,
            needed_bytes=None,
            available_bytes=budget_bytes,
            route=inputs.route,
            message="no build-phase parent table to price; capacity check passes.",
            cap_bytes=budget_bytes,
        )

    binding = worst_warn if worst_warn is not None else worst_fail
    _, table, floor_bytes, cap_bytes = binding
    incoming = inputs.incoming_edge_counts.get(table, 0)
    needed_bytes = _mem.declared_minimum_ceiling_bytes(
        floor_bytes, incoming_edges=incoming, sink=inputs.sink
    )
    warned = worst_warn is not None
    if warned:
        floor_gib = floor_bytes / (1024**3)
        cap_gib = cap_bytes / (1024**3)
        recommend_gib = needed_bytes / (1024**3)
        message = (
            f"out-of-core memory advisory: predicted resident floor ~{floor_gib:.2f} GiB for "
            f"table {table!r} (actual build cap ~{cap_gib:.2f} GiB); recommend a host/cgroup "
            f"ceiling of >= {recommend_gib:.0f} GB for margin."
        )
    else:
        message = "capacity check passes; no table nears its build cap."
    return CapacityEstimate(
        verdict=CapacityVerdict.FIT,
        code=None,
        needed_bytes=needed_bytes,
        available_bytes=budget_bytes,
        route=inputs.route,
        message=message,
        warned=warned,
        binding_table=table,
        floor_bytes=floor_bytes,
        cap_bytes=cap_bytes,
    )


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


def enforce_ooc_memory_preflight(
    parent_table_rows: Mapping[str, int],
    *,
    budget_bytes: int | None,
    sink: bool,
    incoming_edge_counts: Mapping[str, int],
) -> MemoryPreflight:
    """The out-of-core route's memory capacity gate -- HARD-FAILS on an
    un-sizeable fan-in split, WARNS (never blocks) on an adverse build-floor
    prediction. Round-4: the build-floor half of this gate is advisory, not
    hard-fail (`_memory_estimate` module docstring item 2) -- the measured
    completion caps showed the prior flat refusal over-rejected jobs that
    would have completed, so Cam's governing decision changed from "never
    OOM, or declare minimums" to "declare minimums, refuse only what truly
    cannot be sized". Fan-in stays hard: there is nothing to recommend
    around it, only a smaller job or a bigger budget.

    Deliberately asymmetric with the sibling disk preflight (advisory-only,
    `_spill_estimate.enforce_ooc_disk_preflight`) in its FAN-IN half only:
    an under-predicted disk estimate still hits the runtime
    `check_temp_disk_budget` backstop and aborts cleanly, and a fan-in split
    that cannot fit even DuckDB's 1 MB minimum has no such backstop -- so
    fan-in must actually reject before any DuckDB work opens.

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
    split, before any DuckDB work -- the ONE case this still HARD-FAILS.
    `floor(t) > cap(t)` for any `t` (binding table = `argmax_t (floor(t) -
    cap(t))`) no longer hard-fails (round-4): it WARNS with the same
    recommended-size math, and the job proceeds. Below that, it WARNS
    (never blocks) if `floor(t) >= _OOC_MEM_WARN_FRACTION * cap(t)` for the
    tightest such table -- both bands now share the same "warn, never
    block" outcome, differing only in message. `budget_bytes=None`
    (host-RAM detection failed, no explicit budget, mirroring
    `resolve_ooc_memory_limit`) fails OPEN: Part A's caps fall back to the
    flat `memory_limit` here too, so no real per-table cap is left to gate
    a floor against.

    A pure-joiner leaf (incoming edges only, no build) has no `floor(t)`,
    so it is not in `parent_table_rows`; its OWN fan-in is still guarded --
    round-4 moved this guard to run FIRST inside `evaluate_capacity`, ahead
    of the build-phase loop (see that function's own docstring for why: it
    needs only the budget, not a priced row count, so it runs before the
    unresolved-rows / budget-None checks too). Every table in
    `incoming_edge_counts` (leaves included) has its joiner's
    `actual_duckdb_cap_bytes` called to trigger the shared guard, at the
    JOINER split -- a DIFFERENT number than build-phase `live(t)` above on
    the sink path (`1`), so this is a distinct check from the build loop's
    own redundant fan-in catch.

    R1 anti-drift: the floor/cap loop above is no longer inline here -- it
    is `evaluate_capacity`, a pure function this gate and the estimate-only
    `estimate_job_capacity` (CLI `decoy preflight`) both call on the SAME
    typed `CapacityInputs`, so the two can never independently drift on what
    "fits" means. This function is now a thin translation shim: build the
    inputs, ask the evaluator, raise on `INSUFFICIENT` (unchanged caller
    contract -- every existing test in this module keeps passing against
    the same codes/messages), otherwise log the advisory (if any) and
    reconstruct the pre-existing `MemoryPreflight` shape from the evaluator's
    answer.
    """
    inputs = CapacityInputs(
        route=_OUT_OF_CORE_ROUTE,
        parent_table_rows=parent_table_rows,
        incoming_edge_counts=incoming_edge_counts,
        sink=sink,
    )
    estimate = evaluate_capacity(inputs, budget_bytes)
    if estimate.verdict is CapacityVerdict.INSUFFICIENT:
        # An INSUFFICIENT estimate always names its refusal code; the `or`
        # fallback is a defensive type-narrowing (code is str | None) that
        # never actually fires, so mypy sees a str without a banned `assert`.
        raise ExecutionError(
            code=estimate.code or "out_of_core_insufficient_memory",
            message=estimate.message,
        )
    if estimate.warned:
        _logger.warning(estimate.message)
    # NB: in the fits-but-no-warn case this now returns the real
    # binding_table/floor_bytes/cap_bytes from evaluate_capacity, where the
    # pre-extraction code returned (None, 0, budget_bytes). The sole caller
    # (_pipeline_route_exec.py) discards the return, so this is a deliberate,
    # more-informative struct; a future consumer should not assume the old
    # null/zero shape.
    return MemoryPreflight(
        ok=True,
        warned=estimate.warned,
        detectable=budget_bytes is not None,
        binding_table=estimate.binding_table,
        floor_bytes=estimate.floor_bytes,
        cap_bytes=estimate.cap_bytes,
    )

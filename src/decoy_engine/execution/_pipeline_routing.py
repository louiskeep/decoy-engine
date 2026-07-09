"""`run_pipeline`'s execution-route decisions, split out to hold the
600-LOC orchestration cap (CLAUDE.md section "Engineering best practices").

Two independent routing layers compose here, in a fixed decision order
`run_pipeline` enforces by calling them in sequence:

1. S2 relationship routing + SC2 out-of-core auto-routing:
   `decide_execution_route` is the SINGLE live router for
   relationship-bearing (FK) jobs -- it runs first and early-returns
   before the layer-2 chunk classifier is ever consulted for routing.
   For a relationship-bearing PURE-MASK job (no generate tables, no
   validators, no fidelity_report, no vault_writer -- see
   `_sequential_eligible`) under `execution_mode="auto"` it picks, in
   priority order (SC2):

     a. **out_of_core** -- the bounded-RAM DuckDB route -- when the job is
        out-of-core-compatible (`check_out_of_core_compatibility` admits
        the plan: an accepted strategy set, an acyclic table-level FK
        graph, no `when` predicate, single-parent-per-child) AND the
        largest mask table is at/above `out_of_core_threshold_rows`.
        `run_out_of_core_route` executes + packages it.
     b. **sequential** -- the bounded-memory (but O(cardinality), so still
        OOM-able at extreme scale) `run_sequential` path -- for a
        pure-mask FK job that is NOT out-of-core-eligible (an unsupported
        strategy, a self-referential / cyclic FK, a multi-parent child)
        or is below the out-of-core size threshold.
        `run_sequential_route` executes + packages it.
     c. **full_frame** -- the adapter-selected `run` path -- for a job with
        no relationships, and for a relationship job disqualified from
        sequential (generate+mask, validators, fidelity, vault,
        non-pandas substrate) that is small enough to be full-frame-safe.

   And it REJECTS before the read/mask step (fail-closed
   `ExecutionError` code `fk_full_frame_oom_risk_rejected`) when a
   relationship job can ONLY run full-frame (neither out-of-core- nor
   sequential-eligible) AND the largest mask table is at/above
   `full_frame_reject_rows` -- the "never a silent OOM" half of GATE-1's
   prevention posture: a too-big job that no bounded route can take is
   diverted to a clean typed error, not left to the OOM-killer.

   `execution_mode="full_frame"` always forces full-frame (even when
   eligible, even when large -- an explicit operator escape hatch that
   bypasses the reject: the operator owns the OOM risk they asked for);
   `execution_mode="sequential"` forces sequential and
   `execution_mode="out_of_core"` forces out-of-core, each raising
   `ConfigError` if the job is not eligible for the forced route
   (fail-closed: never silently ignore an explicit request). The
   sequential path is pandas-only by construction (`run_sequential` is
   typed to `PandasExecutionAdapter`), so it always constructs its own
   adapter regardless of the caller's `substrate` knob; the out-of-core
   route is likewise pandas-oracle-parity by construction.
2. S3 (engine-efficiencies P3) auto-chunk routing: reached only when (1)
   did NOT take the sequential early return -- every path into
   `decide_chunk_route` is either explicitly forced full_frame or was
   found sequential-ineligible, relationship-bearing jobs in particular.
   `classify_job`'s own `chunked` gate independently excludes any job
   with FK edges or a `relationships` block (`_planner._chunked_rejection`),
   so a relationship-bearing job that reaches this layer (e.g.
   generate+mask FK, or an FK job disqualified from sequential by
   validators) cannot be misrouted into the chunked path either -- the
   two layers compose without overlap: (1) owns relationship routing
   (sequential vs. full_frame); (2) owns single-table non-relationship
   routing (chunked vs. full_frame). When the job classifies `chunked`,
   `run_mask_chunked` streams it through `run_mask_pipeline_chunked` in
   `chunk_size_rows`-row slices instead of one full-frame adapter call;
   this is a peak-memory win only, never a semantic change, and every
   eligibility miss fails CLOSED to the exact full-frame path.

Row-error honesty (D7/D8, S1): the chunked route is never eligible for
row-error quarantine -- `run_mask_pipeline_chunked`'s H1 fail-closed
check (`_chunked.py`) raises `RowErrorsFailedError` the moment ANY chunk
reports a row error, so `run_pipeline` never sees row errors to
quarantine on that route (same policy the pre-existing manual chunked
entrypoint already enforced).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.errors import ConfigError
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution._planner import (
    FULL_FRAME_REJECT_ROWS_DEFAULT,
    OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
)
from decoy_engine.execution._sequential import run_sequential

if TYPE_CHECKING:
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "auto_chunk_stamp",
    "decide_chunk_route",
    "decide_execution_route",
    "execution_telemetry",
    "largest_mask_table_rows",
    "out_of_core_admission",
    "out_of_core_routing_signals",
    "run_mask_chunked",
    "run_out_of_core_route",
    "run_sequential_route",
]


def out_of_core_admission(
    plan: Plan,
    *,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
) -> tuple[bool, str | None]:
    """Static out-of-core compatibility check: `(compatible, primary_code)`.

    Delegates to `check_out_of_core_compatibility` (the same fail-closed gate
    SC1/SC2-part-1 hardened and the parity harness pins) so routing and the
    runner's own pre-flight guard cannot disagree on the admitted surface. Pure
    and cheap: a static read of the compiled plan + relationship graph (no
    per-row work), safe to call on the dispatch path. `primary_code` names the
    first rejection so the reject-before-read message and the forced-mode error
    can explain WHY the route declined.
    """
    from decoy_engine.execution._runner import build_work_list, order_work
    from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility

    work = order_work(build_work_list(plan, registry), graph)
    compat = check_out_of_core_compatibility(plan, work, graph)
    return compat.accepted, compat.primary_code


def largest_mask_table_rows(
    caller_sources: dict[str, pa.Table],
    *,
    table_kinds: dict[str, str],
) -> int | None:
    """Row count of the largest RESIDENT mask-kind source, or None if unknown.

    The out-of-core / reject size gate keys off the largest mask table because
    the full-frame FK memory model (docs/relationships-memory-scaling.md §6) is
    linear in rows-per-table and dominated by the widest/tallest resident
    frame. Returns None when no mask source is resident (e.g. a lazy
    `source_loader` path with an empty `sources` dict): the caller then cannot
    size-gate and falls back to the existing sequential/full-frame behavior
    rather than guessing (an explicit `execution_mode='out_of_core'` remains the
    escape hatch on the lazy path). `num_rows` is Arrow array metadata, so this
    is O(tables), not O(rows).
    """
    mask_rows = [
        src.num_rows for name, src in caller_sources.items() if table_kinds.get(name) == "mask"
    ]
    return max(mask_rows) if mask_rows else None


def out_of_core_routing_signals(
    profile: Any,
    *,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    caller_sources: dict[str, pa.Table],
    table_kinds: dict[str, str],
    has_mask_table: bool,
) -> tuple[bool, str | None, int | None]:
    """The `(out_of_core_compatible, reject_code, largest_table_rows)` triple
    `decide_execution_route`'s SC2 gates consume.

    Computed only for relationship jobs with a mask table -- a static plan +
    Arrow-metadata read, so non-FK jobs pay nothing and keep the pre-SC2
    routing. Off that shape the triple is the inert `(False, None, None)`
    default, which makes every SC2 gate a no-op.
    """
    if not (getattr(profile, "relationships", None) and has_mask_table):
        return False, None, None
    compatible, reject_code = out_of_core_admission(plan, registry=registry, graph=graph)
    return compatible, reject_code, largest_mask_table_rows(caller_sources, table_kinds=table_kinds)


def _has_cross_table_fk_cycle(graph: RelationshipGraph) -> bool:
    """True if the table-level FK graph has a cycle across DISTINCT tables.

    Sequential masking orders whole tables (table_topo_order), so a
    cross-table cycle cannot be sequenced; self-edges (self-ref FK) mask
    within one table and are not a table-level cycle (S2 remediation guide
    r3 section 6).
    """
    from collections import defaultdict

    succ: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        if edge.parent_table != edge.child_table:
            succ[edge.parent_table].add(edge.child_table)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for nxt in succ.get(node, ()):  # DFS back-edge = cycle
            c = color.get(nxt, WHITE)
            if c == GRAY or (c == WHITE and visit(nxt)):
                return True
        color[node] = BLACK
        return False

    return any(color.get(n, WHITE) == WHITE and visit(n) for n in list(succ))


def _sequential_eligible(
    profile: Any,
    *,
    has_generate_table: bool,
    validators: list[Any],
    fidelity_report: bool,
    vault_writer: Any,
    resolved_substrate: str = "pandas",
) -> tuple[bool, str]:
    """Decide whether a mask job may take the bounded-memory sequential path.

    Returns (eligible, reason). `reason` is a stable telemetry token; when
    eligible it is "pure_mask_fk", otherwise it names the disqualifier.

    The sequential path streams/evicts table by table, so any run_pipeline
    post-mask step that needs every masked output resident at once
    disqualifies it: job-level validators (compare positionally against all
    sources), the fidelity report, and the token-vault collection. Pure-generate
    and mixed generate+mask jobs are disqualified because generate tables are
    not masked table-by-table through this path.

    `resolved_substrate` (S3 reconciliation, P1 x S2): `run_sequential` is
    pandas-only by construction. An explicit non-pandas `substrate` request
    (or `DECOY_SUBSTRATE=polars` resolving through `substrate=None`) must
    disqualify sequential eligibility rather than silently ignoring the
    request -- the full_frame branch's `select_execution_adapter` is what
    actually honors `substrate="polars"` (native polars execution, or its
    own explicit `fallback_to_pandas` contract, including
    `code='polars_substrate_strategy_unmigrated'` when fallback is
    disabled). Defaults to `"pandas"` so callers that never pass a
    substrate keep today's routing (the common case: a pure-mask FK job is
    bounded-memory by default) and existing unit tests that construct this
    predicate directly without a substrate argument are unaffected.
    """
    if not profile.relationships:
        return False, "no_relationships"
    if has_generate_table:
        return False, "generate_plus_mask"
    if validators:
        return False, "validators_present"
    if fidelity_report:
        return False, "fidelity_report_requested"
    if vault_writer is not None:
        return False, "vault_writer_requested"
    if resolved_substrate != "pandas":
        return False, "non_pandas_substrate_requested"
    return True, "pure_mask_fk"


def execution_telemetry(
    *, route: str, route_reason: str, sink: Any, source_loader: Any, sources_resident: bool
) -> dict[str, Any]:
    """Per-config execution memory telemetry. Honest by construction: it never
    claims bounded input residency unless the caller's Arrow sources are
    actually NOT resident (a lazy source_loader supplied AND no non-empty
    `sources` dict), and never claims streamed outputs unless a sink was
    supplied.

    MEDIUM (S2 remediation guide section 8): `run_pipeline` always builds
    `caller_sources = dict(sources)`, so a non-empty `sources` dict means the
    inputs ARE resident in memory even when a lazy `source_loader` is ALSO
    supplied. `sources_resident` carries that fact in; bounded input
    residency is reported ONLY for the one configuration that actually bounds
    inputs: a lazy loader supplied AND `sources` empty/omitted.
    """
    if route == "full_frame":
        return {
            "execution_mode": "full_frame",
            "route_reason": route_reason,
            "eviction": "none",
            "outputs_streamed": False,
            "loaded_fully_in_memory": True,
        }
    # The bounded-memory streaming routes (`sequential`, `out_of_core`) share
    # the same honesty shape: they evict per table and stream outputs when a
    # sink is supplied. `execution_mode` echoes the actual route so the two are
    # distinguishable in the manifest.
    return {
        "execution_mode": route,
        "route_reason": route_reason,
        "eviction": "per_table",
        "outputs_streamed": sink is not None,
        "loaded_fully_in_memory": sources_resident or source_loader is None,
    }


def decide_execution_route(
    profile: Any,
    *,
    has_generate_table: bool,
    has_mask_table: bool,
    validators: list[Any],
    fidelity_report: bool,
    vault_writer: Any,
    execution_mode: str,
    graph: RelationshipGraph,
    resolved_substrate: str = "pandas",
    out_of_core_compatible: bool = False,
    out_of_core_reject_code: str | None = None,
    largest_table_rows: int | None = None,
    out_of_core_threshold_rows: int = OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
    full_frame_reject_rows: int = FULL_FRAME_REJECT_ROWS_DEFAULT,
) -> tuple[str, str]:
    """Decide `(route, route_reason)` -- `"out_of_core"`, `"sequential"`, or
    `"full_frame"` -- or RAISE a fail-closed reject-before-read.

    Priority (SC2), for a relationship-bearing PURE-MASK job under `auto`:
    out-of-core (when compat-admitted AND large) > sequential (bounded but
    O(cardinality)) > full-frame. A large relationship job that no bounded
    route can take is REJECTED before read (`ExecutionError`
    `fk_full_frame_oom_risk_rejected`) rather than left to OOM full-frame.

    Out-of-core eligibility is a STRICT SUBSET of sequential eligibility: it
    needs the pure-mask FK shape (`_sequential_eligible`), an acyclic
    single-parent FK graph with only supported strategies
    (`out_of_core_compatible`), AND a largest mask table at/above
    `out_of_core_threshold_rows`. So a pure-mask FK job with an unsupported
    strategy, a cyclic/self-referential FK, or below the size threshold keeps
    taking the existing sequential route; only large, fully-supported FK jobs
    divert to streaming.

    `largest_table_rows` is None when no mask source is resident (a lazy
    loader path): the size gates then never fire and routing falls back to the
    pre-SC2 sequential/full-frame decision (an explicit
    `execution_mode='out_of_core'` is the escape hatch there).

    A mutual cross-table FK cycle (A -> B -> A) cannot be ordered by
    table_topo_order, so `auto` must not route it to sequential (it ran
    fine under full_frame before this program; routing it to sequential is
    a functional regression, not a leak -- see
    docs/backlog/s2-fk-leak-remediation-r3-guide.md section 6). A
    self-referencing table (one table, not a cross-table cycle) is NOT
    flagged and still routes normally.

    `resolved_substrate` disqualifies sequential eligibility when it is
    not `"pandas"` (see `_sequential_eligible`'s docstring): an explicit
    `substrate="polars"` (or an env-resolved one) must route to full_frame
    so `select_execution_adapter` -- not the pandas-only sequential path --
    is what actually decides polars-native-vs-fallback and stamps the
    caller-visible `executed_substrate` / `execution_adapter` telemetry.
    """
    eligible, route_reason = _sequential_eligible(
        profile,
        has_generate_table=has_generate_table,
        validators=validators,
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
        resolved_substrate=resolved_substrate,
    )
    cyclic = _has_cross_table_fk_cycle(graph)
    has_relationships = bool(profile.relationships)
    rows = largest_table_rows if largest_table_rows is not None else 0
    # Out-of-core is only auto-selected for the pure-mask FK shape (a strict
    # subset of sequential eligibility) that the compat gate admits and that is
    # large enough for the route's overhead to be worth it. `cyclic` is already
    # excluded by the compat gate (`out_of_core_relationship_cycle_unsupported`),
    # but is re-checked here for defense in depth.
    out_of_core_ready = (
        eligible
        and not cyclic
        and has_mask_table
        and out_of_core_compatible
        and largest_table_rows is not None
        and rows >= out_of_core_threshold_rows
    )

    if execution_mode == "full_frame":
        # Explicit operator escape hatch: force full-frame even when large.
        # Bypasses the reject -- the operator owns the OOM risk they requested.
        return "full_frame", "override_full_frame"

    if execution_mode == "out_of_core":
        # Fail closed, mirroring the sequential override: never silently ignore
        # an explicit request by falling through to another route.
        if not has_mask_table:
            raise ConfigError(
                "execution_mode='out_of_core' requested but the job has no "
                "mask-kind table to run through the out-of-core path."
            )
        if not eligible:
            raise ConfigError(
                f"execution_mode='out_of_core' requested but the job is not "
                f"out-of-core-eligible ({route_reason})."
            )
        if not out_of_core_compatible:
            raise ConfigError(
                f"execution_mode='out_of_core' requested but the job is not "
                f"out-of-core-compatible "
                f"({out_of_core_reject_code or 'unsupported_recipe'}); use "
                f"execution_mode='full_frame' or 'auto'."
            )
        return "out_of_core", "override_out_of_core"

    if execution_mode == "sequential":
        if not eligible:
            raise ConfigError(
                f"execution_mode='sequential' requested but the job is not "
                f"sequential-eligible ({route_reason})."
            )
        if cyclic:
            raise ConfigError(
                "execution_mode='sequential' requested but the FK graph has a "
                "cross-table cycle, which the sequential path cannot order; "
                "use execution_mode='full_frame' or 'auto'."
            )
        if not has_mask_table:
            # NIT (S2 remediation guide section 8): without a mask-kind table
            # the sequential branch in run_pipeline would silently no-op and
            # fall through to full-frame, ignoring the explicit request.
            # Fail closed instead.
            raise ConfigError(
                "execution_mode='sequential' requested but the job has no "
                "mask-kind table to run through the sequential path."
            )
        return "sequential", route_reason

    # "auto"
    if out_of_core_ready:
        return "out_of_core", "out_of_core_large_fk"
    if eligible and not cyclic:
        return "sequential", route_reason
    # Full-frame-bound: no relationships, a cyclic FK graph (eligible), or a
    # relationship job disqualified from sequential. A cyclic pure-mask job
    # keeps its historical `cross_table_cycle` reason.
    full_frame_reason = "cross_table_cycle" if (eligible and cyclic) else route_reason
    # Reject before read (fail-closed): a LARGE relationship job that no bounded
    # route can take would risk a silent full-frame OOM. Divert to a clean typed
    # error instead of the OOM-killer (GATE-1 #4 prevention half). Reaching this
    # branch at reject scale ALREADY implies no bounded route applies: an
    # out-of-core-eligible job (pure-mask FK + compat-admitted) would have been
    # routed to out_of_core above (its size threshold is below the reject
    # threshold), so it never lands here. That is why the condition does NOT
    # re-test `out_of_core_compatible`: the compat GATE can admit an FK structure
    # (edges/strategies) for a job whose SHAPE still bars out-of-core (a
    # generate+mask FK the route cannot generate), and re-testing the flag would
    # wrongly let such a job fall through to a full-frame OOM. No-relationship
    # jobs are never rejected here: a large flat single table is the layer-2
    # auto-chunk route's concern, not a full-frame FK OOM.
    if has_relationships and largest_table_rows is not None and rows >= full_frame_reject_rows:
        raise ExecutionError(
            code="fk_full_frame_oom_risk_rejected",
            message=(
                f"FK job rejected before read: largest mask table has {rows:,} "
                f"rows, at or above the full-frame reject threshold "
                f"({full_frame_reject_rows:,}); full-frame FK masking at this "
                f"scale would risk an out-of-memory kill. No bounded route "
                f"applies -- the job is not out-of-core-eligible "
                f"({out_of_core_reject_code or 'not a pure-mask FK recipe'}) and "
                f"not sequential-eligible ({full_frame_reason}). Reduce the job "
                f"size, make it out-of-core-eligible (supported strategies + an "
                f"acyclic single-parent FK graph), or force "
                f"execution_mode='full_frame' to override at your own memory risk."
            ),
        )
    return "full_frame", full_frame_reason


def run_sequential_route(
    *,
    plan: Plan,
    loader: Callable[[str], pa.Table],
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    namespace_registry: Any,
    sink: TransactionalSink | None,
    quarantine_config: dict[str, Any] | None,
    route_reason: str,
    source_loader: Callable[[str], pa.Table] | None,
    sources_resident: bool,
    table_kinds: dict[str, str],
    fpe_chunk_count: int = 4,
    explain_plan: bool = False,
    execution_plan_decision: ExecutionPlan | None = None,
) -> ExecutionResult:
    """Execute the sequential route and package it as a full `ExecutionResult`.

    `run_sequential` (Part 2 of S2) is the sole owner of row-error
    enforcement on this route (per-table drain -> raise -> quarantine
    BEFORE write/commit/eviction), so there is no double-processing with
    `run_pipeline`'s full-frame D8 quarantine block -- that block is never
    reached on this route (`run_pipeline` returns immediately with this
    function's result).

    `fpe_chunk_count` (S3 reconciliation, P1 x S2): `decide_execution_route`
    only lets this route run when the resolved substrate is `"pandas"`, so
    the FPE-parallelism knob (which, per `select_execution_adapter`, DOES
    apply to the pandas adapter, unlike `max_workers` / `fallback_to_pandas`
    which are polars-only) is threaded into the constructor here rather
    than silently reverting to `PandasExecutionAdapter`'s class default.

    `explain_plan` / `execution_plan_decision` (S3 reconciliation, P2 x S2):
    `run_pipeline` computes the planner classification BEFORE this route's
    early return specifically so a relationship-route-deferred FK job (this
    route's own eligible shape) still gets an `execution_plan` explain stamp
    -- without this, `explain_plan=True` would silently produce no
    classification at all for exactly the jobs where "why didn't this take
    a faster mode" (relationship-route DEFERRED) is most informative.
    """
    seq_result = run_sequential(
        PandasExecutionAdapter(fpe_chunk_count=fpe_chunk_count),
        plan,
        loader,
        registry=registry,
        relationship_graph=graph,
        namespace_registry=namespace_registry,
        sink=sink,
        quarantine_config=quarantine_config,
    )
    seq_quality_metrics = dict(seq_result.quality_metrics)
    seq_quality_metrics["execution"] = execution_telemetry(
        route="sequential",
        route_reason=route_reason,
        sink=sink,
        source_loader=source_loader,
        sources_resident=sources_resident,
    )
    if explain_plan and execution_plan_decision is not None:
        seq_quality_metrics["execution_plan"] = {
            "mode": execution_plan_decision.mode,
            "reason": execution_plan_decision.reason,
            "rejections": dict(execution_plan_decision.rejections),
        }
    return ExecutionResult(
        outputs=dict(seq_result.outputs),  # {} when a sink was provided
        timings=seq_result.timings,
        boundary_conversion_ms=seq_result.boundary_conversion_ms,
        warnings=seq_result.warnings,
        quality_metrics=seq_quality_metrics,
        table_kinds=table_kinds,
        row_errors=seq_result.row_errors,
    )


def run_out_of_core_route(
    *,
    plan: Plan,
    sources: dict[str, pa.Table],
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    sink: TransactionalSink | None,
    route_reason: str,
    table_kinds: dict[str, str],
    source_loader: Callable[[str], pa.Table] | None,
    sources_resident: bool,
    budget_bytes: int | None = None,
    explain_plan: bool = False,
    execution_plan_decision: ExecutionPlan | None = None,
) -> ExecutionResult:
    """Execute the out-of-core FK route and package it as an `ExecutionResult`.

    Dispatches `run_fk_out_of_core` (the SC1 bounded-batch DuckDB runner). Its
    own pre-flight `check_out_of_core_compatibility` re-asserts admissibility
    fail-closed, so a routing/compat drift raises a coded `ExecutionError`
    rather than producing divergent output -- the route stays pandas-oracle
    byte-parity for every admitted plan (`tests/parity/test_out_of_core_fk_parity.py`).

    Budget: a host-sized `memory_limit` + `batch_rows` are resolved from
    `resolve_budget` so DuckDB is bounded regardless of table cardinality. If
    host-RAM detection fails (`out_of_core_memory_detection_failed`), the route
    still runs on its pinned batch default + DuckDB's own default limit rather
    than newly failing a job full-frame would have completed; a caller that
    needs a hard cap passes an explicit `budget_bytes` (which never falls back).

    Residency: with a `sink` the runner streams bounded batches (outputs `{}`,
    the sink holds the deliverable); without one it reassembles resident tables
    (still bounded per-table on the DuckDB side, but the Python outputs are held
    -- the same resident-vs-streamed distinction the sequential route makes).
    `strategy` surface is SC1's `hash/redact/truncate/passthrough`; widening is
    SC3/SC4, and an unsupported strategy is a routing miss (the job never
    reaches here -- it stays sequential/full-frame), never a run failure.
    """
    from decoy_engine.execution.out_of_core import resolve_budget, run_fk_out_of_core

    memory_limit: str | None = None
    batch_rows: int | None = None
    try:
        budget = resolve_budget(budget_bytes)
        memory_limit, batch_rows = budget.memory_limit, budget.batch_rows
    except ExecutionError:
        # Host-RAM detection failed and no explicit budget was given: fall back
        # to the route's pinned batch default + DuckDB's default limit rather
        # than rejecting a job the in-memory path would have run.
        if budget_bytes is not None:
            raise

    ooc_result = run_fk_out_of_core(
        plan,
        sources,
        registry=registry,
        relationship_graph=graph,
        sink=sink,
        memory_limit=memory_limit,
        batch_rows=batch_rows,
    )
    quality_metrics = dict(ooc_result.quality_metrics)
    quality_metrics["execution"] = execution_telemetry(
        route="out_of_core",
        route_reason=route_reason,
        sink=sink,
        source_loader=source_loader,
        sources_resident=sources_resident,
    )
    if explain_plan and execution_plan_decision is not None:
        quality_metrics["execution_plan"] = {
            "mode": execution_plan_decision.mode,
            "reason": execution_plan_decision.reason,
            "rejections": dict(execution_plan_decision.rejections),
        }
    return ExecutionResult(
        outputs=dict(ooc_result.outputs),  # {} when a sink was provided
        timings=ooc_result.timings,
        boundary_conversion_ms=ooc_result.boundary_conversion_ms,
        warnings=ooc_result.warnings,
        quality_metrics=quality_metrics,
        table_kinds=table_kinds,
        row_errors=ooc_result.row_errors,
    )


def decide_chunk_route(
    config: dict[str, Any],
    *,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    substrate: str,
    caller_sources: dict[str, pa.Table],
    auto_chunk_threshold_rows: int,
    explain_plan: bool,
    auto_chunk: bool,
    has_mask_table: bool,
) -> tuple[ExecutionPlan | None, bool]:
    """Auto-chunk go/no-go + explain surfacing.

    ONE `classify_job` call serves both the routing decision and the
    `execution_plan` explain stamp so the explain surface can never
    disagree with what actually ran. The kill switch (`auto_chunk=False`)
    skips classification entirely unless explain asks: a forced
    full-frame run must not depend on planner behavior.

    Returns `(execution_plan_decision, route_chunked)`; `decision` is
    `None` when neither `explain_plan` nor `auto_chunk` asked for a
    classification.
    """
    if not (explain_plan or (auto_chunk and has_mask_table)):
        return None, False

    from decoy_engine.execution._planner import classify_job

    decision = classify_job(
        config,
        plan=plan,
        registry=registry,
        relationship_graph=graph,
        substrate=substrate,
        source_tables=caller_sources,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
    )
    route_chunked = auto_chunk and decision.mode == "chunked"
    return decision, route_chunked


def run_mask_chunked(
    config: dict[str, Any],
    source: pa.Table,
    *,
    table: str,
    engine_version: str,
    registry: ProviderRegistry,
    adapter: Any,
    vault_writer: Any,
    chunk_size_rows: int,
) -> tuple[dict[str, pa.Table], tuple, float, tuple]:
    """Mask one eligible table via the chunked entrypoint.

    Returns `(outputs, timings, boundary_conversion_ms, warnings)` so the
    routed ExecutionResult keeps the same surface as the full-frame one:
    warnings are the order-stable union of per-chunk warnings, timings a
    per-(strategy, column) rollup, conversion the per-chunk sum. Row
    errors are NOT part of the return: `run_mask_pipeline_chunked`'s H1
    fail-closed check raises `RowErrorsFailedError` the moment any chunk
    reports one, so a normal return here is row-error-free by
    construction (see this module's docstring).

    Slicing is zero-copy (`pa.Table.slice` shares buffers), so the only
    per-chunk materialization is the adapter's pandas working set --
    that bound is the whole point of the route. `concat_masked_chunks`
    concatenates WITHOUT type promotion: the eligibility gates guarantee
    chunk-stable schemas, so any disagreement is a gate miss and raises
    a coded error instead of silently widening (the sole exception, an
    all-null chunk's null-typed column, is cast to the type the other
    chunks agree on -- the same place whole-frame inference lands).
    `combine_chunks` returns one contiguous table so downstream writers
    see the same batch layout as the full-frame path.
    """
    from decoy_engine.execution import _chunked

    def _slices() -> Any:
        for start in range(0, source.num_rows, chunk_size_rows):
            yield source.slice(start, chunk_size_rows)

    chunk_results: list[ExecutionResult] = []
    masked_chunks = list(
        _chunked.run_mask_pipeline_chunked(
            config,
            _slices(),
            table=table,
            engine_version=engine_version,
            registry=registry,
            adapter=adapter,
            vault_writer=vault_writer,
            chunk_result_sink=chunk_results,
        )
    )
    masked = _chunked.concat_masked_chunks(masked_chunks, table=table)
    return (
        {table: masked},
        _chunked.aggregate_chunk_timings(chunk_results),
        sum(r.boundary_conversion_ms for r in chunk_results),
        _chunked.aggregate_chunk_warnings(chunk_results),
    )


def auto_chunk_stamp(
    *,
    route_chunked: bool,
    auto_chunk: bool,
    chunk_size_rows: int,
    auto_chunk_threshold_rows: int,
    table_kinds: dict[str, str],
    caller_sources: dict[str, pa.Table],
    decision: ExecutionPlan | None,
) -> dict[str, Any]:
    """Build the `quality_metrics["auto_chunk"]` reproducibility block."""
    mask_names = [name for name, kind in table_kinds.items() if kind == "mask"]
    source_rows: int | None = None
    if len(mask_names) == 1 and mask_names[0] in caller_sources:
        source_rows = caller_sources[mask_names[0]].num_rows
    chunk_count: int | None = None
    if route_chunked and source_rows is not None:
        chunk_count = -(-source_rows // chunk_size_rows)
    if route_chunked and decision is not None:
        reason = decision.reason
    elif not auto_chunk:
        reason = "auto_chunk disabled; full-frame path forced by the kill switch"
    elif decision is not None:
        reason = decision.rejections.get(
            "chunked", f"planner selected {decision.mode}: {decision.reason}"
        )
    else:  # unreachable by construction; kept total for safety
        reason = "no routing decision was computed"
    return {
        "mode": "chunked" if route_chunked else "full_frame",
        "chunk_size_rows": chunk_size_rows,
        "threshold_rows": auto_chunk_threshold_rows,
        "source_rows": source_rows,
        "chunk_count": chunk_count,
        "reason": reason,
    }

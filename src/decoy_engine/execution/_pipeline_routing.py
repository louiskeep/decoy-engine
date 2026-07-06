"""`run_pipeline`'s execution-route decisions, split out to hold the
600-LOC orchestration cap (CLAUDE.md section "Engineering best practices").

Two independent routing layers compose here, in a fixed decision order
`run_pipeline` enforces by calling them in sequence:

1. S2 (engine "Finish Open-Ended Surfaces" program) relationship routing:
   a relationship-bearing PURE-MASK job (no generate tables, no
   validators, no fidelity_report, no vault_writer -- see
   `_sequential_eligible`) is, by default (`execution_mode="auto"`),
   routed through the bounded-memory `run_sequential` path instead of
   the full-frame adapter-selected `run` path. `decide_execution_route`
   returns the route; `run_sequential_route` executes + packages it as
   a complete `ExecutionResult` (an early return in `run_pipeline`).
   `execution_mode="full_frame"` always forces full-frame (even when
   eligible); `execution_mode="sequential"` forces sequential and raises
   `ConfigError` if the job is not eligible (fail-closed: never silently
   ignore an explicit request). The sequential path is pandas-only by
   construction (`run_sequential` is typed to `PandasExecutionAdapter`),
   so it always constructs its own adapter regardless of the caller's
   `substrate` knob.
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
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
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
    "run_mask_chunked",
    "run_sequential_route",
]


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
    return {
        "execution_mode": "sequential",
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
) -> tuple[str, str]:
    """Decide `(route, route_reason)` -- `"sequential"` or `"full_frame"`.

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
    if execution_mode == "full_frame":
        return "full_frame", "override_full_frame"
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
    if eligible and cyclic:
        return "full_frame", "cross_table_cycle"
    return ("sequential" if eligible else "full_frame"), route_reason


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

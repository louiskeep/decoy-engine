"""Route EXECUTORS for `run_pipeline`'s routing decisions, split out from
`_pipeline_routing` to hold the 600-LOC orchestration cap (CLAUDE.md
"Engineering best practices").

`_pipeline_routing` owns the DECISION of which route a job takes
(`decide_execution_route` / `decide_chunk_route`); this module owns the
EXECUTION of a route once chosen -- dispatching to the underlying runner
(`run_sequential`, `run_fk_out_of_core`, `run_mask_pipeline_chunked`) and
packaging its result into the caller-facing `ExecutionResult` shape,
including the shared execution-telemetry block every routed result
stamps. `run_pipeline` calls into both modules directly; the decision
module never calls the executor module (decisions do not execute), and
the executor module never calls the decision module (executors do not
re-decide).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution._sequential import run_sequential

if TYPE_CHECKING:
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._readers import LazySource
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "execution_telemetry",
    "run_mask_chunked",
    "run_out_of_core_route",
    "run_sequential_route",
]

# OOC-D: the runtime temp-disk cap (`_budget.check_temp_disk_budget`, enforced
# inside `run_fk_out_of_core` at each table boundary) is sized off free disk
# at dispatch time, not the whole free amount -- concurrent processes/jobs on
# the same host are also writing to the same filesystem, so reserving a slice
# rather than claiming 100% of what `shutil.disk_usage` reports right now
# leaves headroom for exactly that concurrent writer. Matches the spirit of
# `_budget.py`'s own conservative-fraction conventions (`_HOST_RAM_FRACTION`
# etc.) applied to disk instead of memory.
_TEMP_DISK_SAFETY_FRACTION = 0.9


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
    unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
    key_provider: KeyProvider | None = None,
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
        unconfigured_column_policy=unconfigured_column_policy,
        key_provider=key_provider,
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
    sources: Mapping[str, pa.Table | LazySource],
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
    unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
    key_provider: KeyProvider | None = None,
) -> ExecutionResult:
    """Execute the out-of-core FK route and package it as an `ExecutionResult`.

    Dispatches `run_fk_out_of_core` (the SC1 bounded-batch DuckDB runner). Its
    own pre-flight `check_out_of_core_compatibility` re-asserts admissibility
    fail-closed, so a routing/compat drift raises a coded `ExecutionError`
    rather than producing divergent output -- the route stays pandas-oracle
    byte-parity for every admitted plan (`tests/parity/test_out_of_core_fk_parity.py`).

    Budget: a host-sized `memory_limit` + `batch_rows` are resolved from
    `resolve_ooc_memory_limit` (NOT `resolve_budget` -- that function's
    conservative fraction is reserved for the router's full-frame admission
    price and must not be widened; see `_budget.py`'s module docstring) so
    DuckDB gets most of the ceiling while staying bounded regardless of table
    cardinality. The resolved UNDIVIDED `budget.budget_bytes` is threaded into
    `run_fk_out_of_core` alongside the legacy `memory_limit` string (SPRINT-1
    Part A) so each DuckDB connection is capped by its own phase-local
    liveness rather than the flat `_max_concurrent_ooc_instances` global-peak
    divisor -- see `_memory_estimate.py`'s module docstring for the root cause
    this fixes. If host-RAM detection fails (`out_of_core_memory_detection_
    failed`), the route still runs on its pinned batch default + DuckDB's own
    default limit rather than newly failing a job full-frame would have
    completed; a caller that needs a hard cap passes an explicit `budget_bytes`
    (which never falls back).

    Memory preflight (SPRINT-1 Part B, the never-crash guarantee): before any
    DuckDB work, `enforce_ooc_memory_preflight` predicts each build-phase
    table's relation-build resident floor (`_parent_table_row_counts`) and
    gates it against the EXACT cap that table's build connection will
    receive -- `resolved_budget_bytes` undivided on the sink path, `//
    (incoming_edges + 1)` on the resident path, the SAME `resolve_ooc_memory_
    limit` call and the SAME arithmetic `resolve_phase_memory_limits` uses to
    size the real connection, reused rather than re-derived. Either warns
    (near a table's own cap) or HARD-FAILS (`out_of_core_insufficient_
    memory`, a table's floor exceeding its own cap) -- unlike the disk
    preflight below, this one actually rejects, because a resident-memory
    floor above its cap has no runtime backstop (DuckDB's own allocator
    raises an uncatchable-at-a-clean-boundary "bad allocation", not a coded
    error at a table boundary). Gating against a fraction of the raw
    detected ceiling instead of this exact cap is the denomination mismatch
    this sprint's remediation closes (a preflight fraction and Part A's real
    per-table cap were never guaranteed to be the same number, so a job could
    be admitted then starved). See `_memory_estimate.py`'s module docstring.

    Disk (OOC-D): `temp_disk_budget_bytes` is threaded into `run_fk_out_of_core`
    as `_TEMP_DISK_SAFETY_FRACTION` (0.9) of the free space `shutil.disk_usage`
    reports at `_spill_estimate.default_ooc_temp_root()` -- the SAME root the
    runner's own `tempfile.mkdtemp` default lands under absent an explicit
    `temp_dir`, so this budget and the runner's actual spill target are the
    same filesystem. This is what makes the runner's existing `check_temp_
    disk_budget` runtime cap (already called at each table boundary once a
    budget is non-`None`) actually enforced on THIS route -- before OOC-D it
    was only ever exercised by callers that pass `temp_disk_budget_bytes`
    directly. An undetectable free-disk count (`OSError`, e.g. an unsupported
    filesystem) leaves the budget `None` rather than newly failing a job the
    route would otherwise have run -- the same fail-open posture the
    `memory_limit` resolution above takes on an undetectable host-RAM read.
    This runtime cap is the ENFORCER of OOC-D (aborts cleanly at a table
    boundary if disk runs short); the separate up-front
    `_spill_estimate.enforce_ooc_disk_preflight` (wired into `_pipeline_
    routing_signals.resolve_execution_route`) is ADVISORY only -- it warns,
    it never rejects.

    Residency: with a `sink` the runner streams bounded batches (outputs `{}`,
    the sink holds the deliverable); without one it reassembles resident tables
    (still bounded per-table on the DuckDB side, but the Python outputs are held
    -- the same resident-vs-streamed distinction the sequential route makes).
    `strategy` surface is SC1's `hash/redact/truncate/passthrough`; widening is
    SC3/SC4, and an unsupported strategy is a routing miss (the job never
    reaches here -- it stays sequential/full-frame), never a run failure.

    `source_loader` resolution (M1 fix): `run_fk_out_of_core` needs its whole
    `sources` mapping upfront -- unlike the sequential route, the DuckDB batch
    runner has no incremental per-table eviction loop to hang a lazy load off
    of. So a forced `execution_mode='out_of_core'` on a truly lazy job
    (`sources={}`, `source_loader` supplied) resolves every table
    `run_sequential`'s own lazy contract would have loaded
    (`table_topo_order(plan, graph)`: every plan table plus every graph-edge
    table) through `source_loader` before dispatch, mirroring
    `run_sequential_route`'s per-table loader contract instead of raising
    `out_of_core_source_missing`. This does cost full per-table residency for
    whichever tables were newly resolved this way (no incremental eviction on
    this route); the runner's own bounded `batch_rows` streaming -- its actual
    peak-memory guarantee during masking/join -- is unaffected either way.
    """
    from decoy_engine.execution._sequential import table_topo_order
    from decoy_engine.execution.out_of_core import resolve_ooc_memory_limit, run_fk_out_of_core
    from decoy_engine.execution.out_of_core._memory_estimate import enforce_ooc_memory_preflight
    from decoy_engine.execution.out_of_core._spill_estimate import default_ooc_temp_root

    resolved_sources = sources
    if source_loader is not None:
        missing = [table for table in table_topo_order(plan, graph) if table not in sources]
        if missing:
            resolved_sources = dict(sources)
            for table in missing:
                resolved_sources[table] = source_loader(table)

    memory_limit: str | None = None
    batch_rows: int | None = None
    resolved_budget_bytes: int | None = None
    try:
        # `resolve_ooc_memory_limit`, NOT `resolve_budget`: this is the
        # DuckDB memory_limit for the execution runner, not the router's
        # full-frame admission price (`_pipeline_routing_signals.py` keeps
        # calling `resolve_budget` directly for that, unchanged -- see
        # `_budget.py`'s module docstring for why the two must stay
        # decoupled). `max_concurrent_instances` is computed exactly from
        # THIS job's own relationship graph, not left at the module's
        # conservative default -- the graph is already in hand here.
        budget = resolve_ooc_memory_limit(
            budget_bytes,
            max_concurrent_instances=_max_concurrent_ooc_instances(graph, sink=sink is not None),
        )
        memory_limit, batch_rows = budget.memory_limit, budget.batch_rows
        # SPRINT-1 Part A: the UNDIVIDED total, threaded alongside the legacy
        # flat `memory_limit` so `run_fk_out_of_core` can size each DuckDB
        # connection by its own phase-local liveness instead of the run's
        # single global-peak divisor (see `_memory_estimate.py`). Resolved
        # together with `memory_limit`/`batch_rows` above so the three never
        # disagree about whether resolution succeeded.
        resolved_budget_bytes = budget.budget_bytes
    except ExecutionError:
        # Host-RAM detection failed and no explicit budget was given: fall back
        # to the route's pinned batch default + DuckDB's default limit rather
        # than rejecting a job the in-memory path would have run.
        if budget_bytes is not None:
            raise

    # OOC-D: size the runtime spill cap off free disk at THIS root -- the same
    # one `run_fk_out_of_core` spills under by default -- so `check_temp_disk_
    # budget` (already called at each table boundary inside the runner) is
    # enforced on the pipeline path, not just callers that pass this directly.
    temp_disk_budget_bytes: int | None = None
    try:
        free_bytes = shutil.disk_usage(default_ooc_temp_root()).free
        temp_disk_budget_bytes = int(free_bytes * _TEMP_DISK_SAFETY_FRACTION)
    except OSError:
        # Undetectable free-disk count: leave the runtime cap unset rather than
        # newly blocking a job the route would otherwise have run. This runtime
        # cap is OOC-D's enforcer; the up-front `enforce_ooc_disk_preflight` is
        # advisory (warn-only) and never blocks a job.
        pass

    # SPRINT-1 Part B: the hybrid memory capacity preflight -- the never-crash
    # guarantee underneath Part A's phase-aware caps above. Wired HERE rather
    # than at `_pipeline_routing_signals.resolve_execution_route` (the disk
    # preflight's site): that module sits at its own LOC cap with no headroom,
    # and this site already has per-table row counts in hand via
    # `resolved_sources` (both `pa.Table` and `LazySource` expose `.num_rows`
    # in O(1), so no source is materialized just to count it) -- the same
    # "site with row counts already in hand" precedent the disk preflight
    # itself establishes. Runs strictly before `run_fk_out_of_core` below, so
    # a job whose predicted floor cannot fit is refused before any DuckDB
    # work starts.
    #
    # Gated against `resolved_budget_bytes` -- THE SAME `OutOfCoreBudget.
    # budget_bytes` resolved above at the ONE `resolve_ooc_memory_limit` call
    # site, reused rather than re-derived -- and `sink is not None` /
    # `_incoming_edge_counts(graph)`, so `cap(t)` here is computed with the
    # IDENTICAL arithmetic `resolve_phase_memory_limits` uses to size the
    # real connection. This is the fix for the BLOCKER: a preflight that
    # instead re-derived its own fraction of the raw detected ceiling could
    # admit a job whose real, phase-aware cap was starved -- the fraction and
    # the true cap were never guaranteed to be the same number. Reusing
    # `resolved_budget_bytes` verbatim makes that denomination mismatch
    # structurally impossible: when it is `None` (host-RAM detection failed,
    # no explicit budget given), the gate fails OPEN, matching Part A's own
    # fall-through to the flat `memory_limit` in that same case.
    enforce_ooc_memory_preflight(
        _parent_table_row_counts(resolved_sources, graph),
        budget_bytes=resolved_budget_bytes,
        sink=sink is not None,
        incoming_edge_counts=_incoming_edge_counts(graph),
    )

    ooc_result = run_fk_out_of_core(
        plan,
        resolved_sources,
        registry=registry,
        relationship_graph=graph,
        sink=sink,
        memory_limit=memory_limit,
        batch_rows=batch_rows,
        budget_bytes=resolved_budget_bytes,
        temp_disk_budget_bytes=temp_disk_budget_bytes,
        unconfigured_column_policy=unconfigured_column_policy,
        key_provider=key_provider,
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


def _incoming_edge_counts(graph: RelationshipGraph) -> dict[str, int]:
    """Fan-in per table: the count of edges where the table is the CHILD
    side, i.e. how many `ChildFkBatchJoiner` connections are co-live while
    that table streams.

    Shared by `_max_concurrent_ooc_instances` (the run's single global-peak
    divisor) and `enforce_ooc_memory_preflight` (each table's OWN resident-
    path build cap, `_memory_estimate` module docstring) -- both need this
    same per-table fan-in, computed once from the graph rather than twice.
    """
    counts: dict[str, int] = {}
    for edge in graph.edges:
        counts[edge.child_table] = counts.get(edge.child_table, 0) + 1
    return counts


def _max_concurrent_ooc_instances(graph: RelationshipGraph, *, sink: bool) -> int:
    """Exact upper bound on concurrent full-budget DuckDB instances, sizing
    `resolve_ooc_memory_limit`'s `max_concurrent_instances` from a job's own
    graph instead of that function's conservative default.

    One table streams at a time, holding one `ChildFkBatchJoiner` per INCOMING
    edge. Sink path: `emit_to_sink` closes every joiner (`on_stream_consumed`
    in `_emit.py`) BEFORE the relation build opens, so joiners and build never
    co-live -- the peak is whichever single phase is wider, max(incoming, 1).
    Resident path (no sink): joiners stay open THROUGH the build, so they sum
    to incoming + 1. `sink` selects the correct model; returning the resident
    peak for a sink job over-counts liveness that never exists, which
    `resolve_ooc_memory_limit`'s fail-closed fan-in guard then reads as a false
    refusal. The run's worst case is the max over every table the graph touches
    -- a schema-level property, safe to compute once up front.
    """
    incoming_counts = _incoming_edge_counts(graph)
    has_outgoing: set[str] = {edge.parent_table for edge in graph.edges}
    tables = set(incoming_counts) | has_outgoing
    if not tables:
        return 1

    def _peak(table: str) -> int:
        incoming = incoming_counts.get(table, 0)
        build = 1 if table in has_outgoing else 0
        return max(incoming, build) if sink else incoming + build

    return max(_peak(table) for table in tables)


def _parent_table_row_counts(
    sources: Mapping[str, pa.Table | LazySource], graph: RelationshipGraph
) -> dict[str, int]:
    """One row count per table with an outgoing FK edge (a build-phase
    table), feeding `enforce_ooc_memory_preflight`'s per-table floor
    prediction.

    Parent ROW COUNT (not distinct-key count) is priced because it is a safe
    upper bound on the relation-build's true cardinality (distinct keys can
    never exceed rows) and is available without touching column data: both
    `pa.Table` and `LazySource` expose `.num_rows` in O(1) (an in-memory
    attribute, or a Parquet footer read that never scans row-group data).

    FAIL-CLOSED, not a silent under-count: a graph parent table absent from
    `sources` used to simply contribute 0 rows, which UNDER-predicts the
    floor -- admitting a job the preflight should have refused is exactly
    the wrong direction for a gate whose only job is refusing before an OOM
    (LOW remediation). `run_fk_out_of_core` does raise its own coded
    `out_of_core_source_missing` for the same gap, but only AFTER this
    preflight would have already (wrongly) admitted the job on a stale
    ordering guarantee; this function raises fail-closed itself instead of
    depending on that.
    """
    parent_tables = {edge.parent_table for edge in graph.edges}
    rows: dict[str, int] = {}
    for table in parent_tables:
        source = sources.get(table)
        if source is None:
            raise ExecutionError(
                code="out_of_core_parent_rows_unresolved",
                message=(
                    f"out-of-core memory preflight cannot price table {table!r}: it has an "
                    "outgoing FK edge but no resolvable source."
                ),
            )
        rows[table] = source.num_rows
    return rows


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
    key_provider: KeyProvider | None = None,
) -> tuple[dict[str, pa.Table], tuple, float, tuple]:
    """Mask one eligible table via the chunked entrypoint.

    Returns `(outputs, timings, boundary_conversion_ms, warnings)` so the
    routed ExecutionResult keeps the same surface as the full-frame one:
    warnings are the order-stable union of per-chunk warnings, timings a
    per-(strategy, column) rollup, conversion the per-chunk sum. Row
    errors are NOT part of the return: `run_mask_pipeline_chunked`'s H1
    fail-closed check raises `RowErrorsFailedError` the moment any chunk
    reports one, so a normal return here is row-error-free by
    construction (see `_pipeline_routing`'s module docstring).

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
            key_provider=key_provider,
        )
    )
    masked = _chunked.concat_masked_chunks(masked_chunks, table=table)
    return (
        {table: masked},
        _chunked.aggregate_chunk_timings(chunk_results),
        sum(r.boundary_conversion_ms for r in chunk_results),
        _chunked.aggregate_chunk_warnings(chunk_results),
    )

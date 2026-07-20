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
    cardinality, divided across the graph's exact worst-case concurrent
    DuckDB-instance count (`_max_concurrent_ooc_instances`). If host-RAM
    detection fails (`out_of_core_memory_detection_failed`), the route still
    runs on its pinned batch default + DuckDB's own default limit rather than
    newly failing a job full-frame would have completed; a caller that needs
    a hard cap passes an explicit `budget_bytes` (which never falls back).

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

    resolved_sources = sources
    if source_loader is not None:
        missing = [table for table in table_topo_order(plan, graph) if table not in sources]
        if missing:
            resolved_sources = dict(sources)
            for table in missing:
                resolved_sources[table] = source_loader(table)

    memory_limit: str | None = None
    batch_rows: int | None = None
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
            budget_bytes, max_concurrent_instances=_max_concurrent_ooc_instances(graph)
        )
        memory_limit, batch_rows = budget.memory_limit, budget.batch_rows
    except ExecutionError:
        # Host-RAM detection failed and no explicit budget was given: fall back
        # to the route's pinned batch default + DuckDB's default limit rather
        # than rejecting a job the in-memory path would have run.
        if budget_bytes is not None:
            raise

    ooc_result = run_fk_out_of_core(
        plan,
        resolved_sources,
        registry=registry,
        relationship_graph=graph,
        sink=sink,
        memory_limit=memory_limit,
        batch_rows=batch_rows,
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


def _max_concurrent_ooc_instances(graph: RelationshipGraph) -> int:
    """Exact worst-case count of simultaneously-live DuckDB instances for
    `run_fk_out_of_core` over `graph`, sizing `resolve_ooc_memory_limit`'s
    `max_concurrent_instances` precisely instead of falling back to that
    function's conservative default.

    Mirrors `_budget.py`'s module-docstring analysis of `_runner.py`: only
    one table streams at a time, but while it streams, `_stream_table` holds
    one `ChildFkBatchJoiner` connection open per INCOMING edge, and (while
    those are still open) builds one further connection at a time for each
    OUTGOING edge's parent-key relation. So one table's worst case is its
    incoming-edge count plus one (if it has any outgoing edge at all; a leaf
    table with no outgoing edge never opens a relation-build connection).
    The run's worst case is the max of that over every table the graph
    touches -- a schema-level property, safe to compute once up front
    (never re-derived per batch or scaled by row count).
    """
    incoming_counts: dict[str, int] = {}
    has_outgoing: set[str] = set()
    for edge in graph.edges:
        incoming_counts[edge.child_table] = incoming_counts.get(edge.child_table, 0) + 1
        has_outgoing.add(edge.parent_table)
    tables = set(incoming_counts) | has_outgoing
    if not tables:
        return 1
    return max(
        incoming_counts.get(table, 0) + (1 if table in has_outgoing else 0) for table in tables
    )


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

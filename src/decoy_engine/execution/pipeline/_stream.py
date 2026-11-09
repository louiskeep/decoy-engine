"""`run_pipeline(execution_mode="stream")`: the strict "must stream" requirement.

`stream` is translated once, at the `run_pipeline` entry, to `auto` plus the immutable
`stream_required` field on the run context. Only `decide_route` reads that field, and hands the
decision to `decide_stream_route`. The routing code below it keeps seeing a mode it already knows.

The decision is a preflight: it reads the validated config, the plan and source footers, and
raises `runtime.streaming_unavailable` before any source passed in `sources` is read when no streaming
route applies. It reuses the existing gates and adds no new admission rule of its own:

- every mask table passes `check_chunked_compatibility`, the complete gate base `--chunked`
  runs per table (strategy admission, both orientations of FK key checks, FK child edges);
- a job whose FK config fails that gate may still run on the out-of-core route, when that
  route's own compatibility holds and the FK dtype hazard is clear;
- the lane, or the B7 per-table split, must take every mask table with a lazy source and a
  streaming sink. The size threshold does not apply.

Pattern: the planner decides, the caller states a requirement, and an unmet requirement is a
coded refusal rather than a silent fallback (Polars falls back silently; B6b chose otherwise,
`_chunked_input`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from decoy_engine import reasons
from decoy_engine.execution._errors import DeclaredCastRouteError, ExecutionError
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.execution._pipeline_context import PipelineRunContext, RouteDecision
    from decoy_engine.execution._pipeline_multi_table import MultiTableSplit
    from decoy_engine.relationships import RelationshipGraph

__all__ = ["StreamVerdict", "decide_stream_route"]


@dataclass(frozen=True)
class StreamVerdict:
    """What a `stream` run decided. `fk_split`: the job has FK edges and every mask table passed
    the complete chunked compatibility gate, so each table runs as its own lane unit."""

    fk_split: bool = False
    # The B7 split the preflight admitted; execution reuses it rather than deciding again.
    split: MultiTableSplit | None = None

    def lane_view(
        self, config: dict[str, Any], graph: RelationshipGraph
    ) -> tuple[dict[str, Any], RelationshipGraph]:
        """The config and graph the lane classifier sees: without the FK graph for an admitted
        FK split (each table masks on its own, exactly as base `--chunked` runs it)."""
        if not self.fk_split:
            return config, graph
        from decoy_engine.relationships import RelationshipGraph as Graph

        return (
            {k: v for k, v in config.items() if k != "relationships"},
            Graph(edges=(), ordering=()),
        )


def _refusal(fact: str) -> ExecutionError:
    return ExecutionError(
        code=reasons.runtime.STREAMING_UNAVAILABLE,
        message=f"execution_mode='stream' needs a streaming route and none applies: {fact}",
    )


def _compat_failure(ctx: PipelineRunContext, mask_tables: list[str]) -> str | None:
    """The first mask table's coded failure of the complete chunked compatibility gate."""
    from decoy_engine.execution._chunked import check_chunked_compatibility

    for table in mask_tables:
        try:
            check_chunked_compatibility(ctx.config, table=table, registry=ctx.registry)
        except PlanCompileError as exc:
            return f"table {table!r}: {exc.code}: {exc.message}"
        except DeclaredCastRouteError as exc:
            # A declared cast the chunked route cannot honour is a compatibility decline like any
            # other; only the code is kept so the refusal stays value-free.
            return f"table {table!r}: {exc.code}"
    return None


def _shape_failure(ctx: PipelineRunContext, mask_tables: list[str]) -> str | None:
    from decoy_engine.execution._transactional_sink import TransactionalSink

    generated = sorted(n for n, k in ctx.table_kinds.items() if k == "generate")
    if generated:
        return f"generate-kind table(s) {', '.join(generated)} present; only mask tables stream"
    if not mask_tables:
        return "the job has no mask table to stream"
    if ctx.sink is None:
        return "no sink was supplied, so the output cannot stream"
    if not isinstance(ctx.sink, TransactionalSink):
        return "the sink does not stream batches"
    if not ctx.auto_chunk:
        return "auto_chunk is disabled for this run"
    return None


def _source_failure(ctx: PipelineRunContext, mask_tables: list[str], kept: Any) -> str | None:
    """A non-empty mask source that is not a lazy source the lane keeps open for streaming."""
    from decoy_engine.execution import _chunked_input

    for table in mask_tables:
        src = ctx.caller_sources.get(table)
        if src is None:
            return f"table {table!r} has no source"
        if table in kept:
            continue
        rows = (
            _chunked_input.source_facts(src).num_rows
            if isinstance(src, LazySource)
            else src.num_rows
        )
        if rows > 0:
            return f"the source of table {table!r} is resident; a lazy source is required"
    return None


def _lane_decision(ctx: PipelineRunContext, mask_tables: list[str], fk: bool) -> RouteDecision:
    from decoy_engine.execution import _pipeline_multi_table as multi
    from decoy_engine.execution import _pipeline_routing
    from decoy_engine.execution._chunked_output_sink import decide_output_mode
    from decoy_engine.execution._pipeline_context import RouteDecision

    verdict = StreamVerdict(fk_split=fk)
    decision, route_chunked, keep_lazy = _pipeline_routing.decide_chunk_route(
        ctx.config,
        plan=ctx.plan,
        registry=ctx.registry,
        graph=ctx.graph,
        substrate=ctx.resolved_substrate,
        caller_sources=ctx.caller_sources,
        auto_chunk_threshold_rows=ctx.auto_chunk_threshold_rows,
        explain_plan=ctx.explain_plan,
        auto_chunk=ctx.auto_chunk,
        has_mask_table=ctx.has_mask_table,
        table_kinds=ctx.table_kinds,
        prepared_tables=ctx.prepared.prepared,
        stream=verdict,
    )
    split = None
    if len(mask_tables) == 1:
        if not route_chunked:
            fact = "chunked routing was declined for this job"
            if decision is not None:
                fact = decision.rejections.get("chunked", decision.reason)
            raise _refusal(fact)
    else:
        split = multi.decide_multi_table_split(
            ctx.config,
            plan=ctx.plan,
            registry=ctx.registry,
            graph=ctx.graph,
            substrate=ctx.resolved_substrate,
            caller_sources=ctx.caller_sources,
            table_kinds=ctx.table_kinds,
            auto_chunk=ctx.auto_chunk,
            source_facts=keep_lazy,
            auto_chunk_threshold_rows=ctx.auto_chunk_threshold_rows,
            dispatcher_enabled=ctx.chunked_dispatcher_enabled,
            split_enabled=ctx.multi_table_dispatch_enabled,
            vault_writer_present=ctx.vault_writer is not None,
            stream=verdict,
        )
        if split is None:
            raise _refusal("the multi-table split does not apply to this job")
        if split.full_frame:
            first = split.full_frame[0]
            raise _refusal(f"table {first!r} cannot stream: {split.reasons[first]}")
    mode, why = decide_output_mode(
        stream_chunked_output=ctx.publish.stream,
        sink=ctx.sink,
        dispatcher_enabled=ctx.chunked_dispatcher_enabled,
        config=ctx.config,
        fidelity_report=ctx.fidelity_report,
        post_validation=ctx.publish.post_validation,
        split=split,
        resident_names=tuple(ctx.caller_sources),
    )
    if mode != "streamed":
        raise _refusal(f"the output stays resident ({why})")
    failure = _source_failure(ctx, mask_tables, keep_lazy)
    if failure is not None:
        raise _refusal(failure)
    return RouteDecision(
        route="full_frame",
        route_reason=reasons.route.CHUNKED_ADMITTED,
        execution_plan_decision=decision,
        route_chunked=route_chunked,
        keep_lazy=keep_lazy,
        ooc_declined=None,
        stream=replace(verdict, split=split),
    )


def _out_of_core_decision(
    ctx: PipelineRunContext,
    mask_tables: list[str],
    chunk_fact: str,
    decide_route: Callable[[PipelineRunContext], RouteDecision],
) -> RouteDecision:
    """The guarded out-of-core force for an FK job the chunked gate rejects: only when that
    route's compatibility holds and the FK dtype hazard is clear, both read from metadata."""
    from decoy_engine.execution._pipeline_routing import out_of_core_routing_signals
    from decoy_engine.execution._pipeline_routing_fk_dtype import fk_out_of_core_dtype_hazard

    ok, code, _rows, _exact = out_of_core_routing_signals(
        ctx.profile,
        plan=ctx.plan,
        registry=ctx.registry,
        graph=ctx.graph,
        caller_sources=ctx.caller_sources,
        table_kinds=ctx.table_kinds,
        has_mask_table=ctx.has_mask_table,
        config=ctx.config,
    )
    if not ok:
        raise _refusal(f"{chunk_fact}; out-of-core is also unavailable ({code or 'unsupported'})")
    if fk_out_of_core_dtype_hazard(ctx.plan, ctx.graph, caller_sources=ctx.caller_sources):
        raise _refusal(f"{chunk_fact}; out-of-core is unavailable (fk_out_of_core_dtype_hazard)")
    lazy = {t for t in mask_tables if isinstance(ctx.caller_sources.get(t), LazySource)}
    failure = _source_failure(ctx, mask_tables, lazy)
    if failure is not None:
        raise _refusal(failure)
    refused_code: str | None = None
    try:
        return decide_route(replace(ctx, execution_mode="out_of_core", stream_required=False))
    except ExecutionError as exc:
        refused_code = exc.code
    # Raised outside the handler so the refusal carries no context from the forced route's error.
    raise _refusal(f"{chunk_fact}; out-of-core refused the job ({refused_code})")


def decide_stream_route(
    ctx: PipelineRunContext, decide_route: Callable[[PipelineRunContext], RouteDecision]
) -> RouteDecision:
    """Routing for a run that requires streaming: a `RouteDecision` for the lane (or the B7
    split) or the forced out-of-core route, else `runtime.streaming_unavailable`."""
    mask_tables = [name for name, kind in ctx.table_kinds.items() if kind == "mask"]
    fact = _shape_failure(ctx, mask_tables)
    if fact is not None:
        raise _refusal(fact)
    chunk_fact = _compat_failure(ctx, mask_tables)
    has_fk = bool(ctx.graph.edges or ctx.config.get("relationships"))
    if chunk_fact is None:
        return _lane_decision(ctx, mask_tables, fk=has_fk)
    if not has_fk:
        raise _refusal(chunk_fact)
    return _out_of_core_decision(ctx, mask_tables, chunk_fact, decide_route)

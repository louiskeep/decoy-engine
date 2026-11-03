"""Context-driven entry points for the two bounded layer-1 routes.

`run_pipeline` dispatches every layer-1 route as `execute(ctx, decision)`. These two adapters
unpack a `PipelineRunContext` + `RouteDecision` into the keyword calls that
`_pipeline_route_exec.run_sequential_route` / `run_out_of_core_route` already take, so those
executors keep their signature (existing tests and `physical` drivers call them directly and
patch them by module attribute; the calls below go through that attribute on purpose).

Nothing here decides or re-decides a route: the decision arrives finished in `RouteDecision`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from decoy_engine.declared_casting._boundary import open_cast_run
from decoy_engine.execution import _pipeline_route_exec as _route_exec
from decoy_engine.execution import _pipeline_sources as _psrc
from decoy_engine.execution._transforms_admission import stamp_out_of_core_declined

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._pipeline_context import PipelineRunContext, RouteDecision

__all__ = [
    "execute_mixed_route",
    "execute_out_of_core_route",
    "execute_route",
    "execute_sequential_route",
]


def execute_route(
    ctx: PipelineRunContext, decision: RouteDecision, refusals: dict[Any, Any]
) -> ExecutionResult:
    """Layer-1 dispatch: every route is an executor over the same (ctx, decision). The result
    carries the declared-route refusals the admission layers noted while deciding."""
    from decoy_engine.declared_casting._refusals import stamp
    from decoy_engine.execution import _pipeline_full_frame

    if ctx.has_mask_table and decision.route == "sequential":
        result = execute_sequential_route(ctx, decision)
    elif ctx.has_mask_table and decision.route == "out_of_core":
        result = execute_out_of_core_route(ctx, decision)
    elif ctx.has_mask_table and decision.route == "mixed":
        result = execute_mixed_route(ctx, decision)
    else:
        result = _pipeline_full_frame.run_full_frame_route(ctx, decision)
    stamp(result.quality_metrics, refusals)
    return result


def execute_sequential_route(ctx: PipelineRunContext, decision: RouteDecision) -> ExecutionResult:
    """Run the sequential route; the out-of-core decline (if any) is stamped on its result."""
    loader = _psrc.resolve_sequential_loader(
        ctx.source_loader, ctx.caller_sources, config=ctx.config, prepared=ctx.prepared.prepared
    )
    cast_run = open_cast_run(ctx.plan, ctx.quarantine_runtime)
    try:
        sequential_result = _sequential_route(ctx, decision, loader, cast_run)
    except BaseException as failure:
        if cast_run is not None:
            cast_run.abort(failure)
        raise
    stamp_out_of_core_declined(sequential_result.quality_metrics, decision.ooc_declined)
    return sequential_result


def _sequential_route(
    ctx: PipelineRunContext, decision: RouteDecision, loader: Any, cast_run: Any
) -> ExecutionResult:
    return _route_exec.run_sequential_route(
        plan=ctx.plan,
        loader=loader,
        registry=ctx.registry,
        graph=ctx.graph,
        namespace_registry=ctx.namespace_registry,
        sink=ctx.sink,
        quarantine_config=ctx.config.get("quarantine"),
        route_reason=decision.route_reason,
        source_loader=ctx.source_loader,
        sources_resident=bool(ctx.caller_sources),
        fpe_chunk_count=ctx.fpe_chunk_count,
        table_kinds=ctx.table_kinds,
        explain_plan=ctx.explain_plan,
        execution_plan_decision=decision.execution_plan_decision,
        unconfigured_column_policy=ctx.projection_policy,
        key_provider=ctx.key_provider,
        native_threads=ctx.native_threads,
        cast_run=cast_run,
    )


def execute_out_of_core_route(ctx: PipelineRunContext, decision: RouteDecision) -> ExecutionResult:
    """Run the out-of-core route. `caller_sources` feeds the runner directly (TB-1: a
    `LazySource` streams natively there, no materialization)."""
    cast_run = open_cast_run(ctx.plan, ctx.quarantine_runtime)
    try:
        return _out_of_core_route(ctx, decision, cast_run)
    except BaseException as failure:
        if cast_run is not None:
            cast_run.abort(failure)
        raise


def _out_of_core_route(
    ctx: PipelineRunContext, decision: RouteDecision, cast_run: Any
) -> ExecutionResult:
    return _route_exec.run_out_of_core_route(
        plan=ctx.plan,
        sources=ctx.caller_sources,
        registry=ctx.registry,
        graph=ctx.graph,
        sink=ctx.sink,
        route_reason=decision.route_reason,
        table_kinds=ctx.table_kinds,
        source_loader=ctx.source_loader,
        sources_resident=bool(ctx.caller_sources),
        budget_bytes=ctx.out_of_core_budget_bytes,
        explain_plan=ctx.explain_plan,
        execution_plan_decision=decision.execution_plan_decision,
        unconfigured_column_policy=ctx.projection_policy,
        key_provider=ctx.key_provider,
        out_of_core_reorder_threshold_rows=ctx.out_of_core_reorder_threshold_rows,
        vault_writer=ctx.vault_writer,
        config=ctx.config,
        native_threads=ctx.native_threads,
        cast_run=cast_run,
    )


def execute_mixed_route(ctx: PipelineRunContext, decision: RouteDecision) -> ExecutionResult:
    """Run the production mixed (generate + mask) route."""
    return _route_exec.run_mixed_route(ctx, decision)

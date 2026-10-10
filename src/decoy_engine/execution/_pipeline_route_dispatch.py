"""Context-driven entry points for the two bounded layer-1 routes.

`run_pipeline` dispatches every layer-1 route as `execute(ctx, decision)`. These two adapters
unpack a `PipelineRunContext` + `RouteDecision` into the keyword calls that
`_pipeline_route_exec.run_sequential_route` / `run_out_of_core_route` already take, so those
executors keep their signature (existing tests and `physical` drivers call them directly and
patch them by module attribute; the calls below go through that attribute on purpose).

Nothing here decides or re-decides a route: the decision arrives finished in `RouteDecision`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from decoy_engine.execution import _pipeline_route_exec as _route_exec
from decoy_engine.execution import _pipeline_sources as _psrc
from decoy_engine.execution._transforms_admission import stamp_out_of_core_declined

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._pipeline_context import PipelineRunContext, RouteDecision

__all__ = ["execute_out_of_core_route", "execute_sequential_route"]


def execute_sequential_route(ctx: PipelineRunContext, decision: RouteDecision) -> ExecutionResult:
    """Run the sequential route; the out-of-core decline (if any) is stamped on its result."""
    loader = _psrc.resolve_sequential_loader(
        ctx.source_loader, ctx.caller_sources, config=ctx.config, prepared=ctx.prepared.prepared
    )
    sequential_result = _route_exec.run_sequential_route(
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
    )
    stamp_out_of_core_declined(sequential_result.quality_metrics, decision.ooc_declined)
    return sequential_result


def execute_out_of_core_route(ctx: PipelineRunContext, decision: RouteDecision) -> ExecutionResult:
    """Run the out-of-core route. `caller_sources` feeds the runner directly (TB-1: a
    `LazySource` streams natively there, no materialization)."""
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
    )

"""`native_stream` driver adapter (Task 4.2, D2).

Wraps `execution._native_route_exec.try_native_route`: an admission +
execution BRIDGE, not a post-selection driver. Its result explicitly permits
decline -- `(None, report)` for every reroute (static decline: no source
touched; dynamic decline: exactly one batch read then discarded) -- and this
adapter forwards that shape unchanged. The coordinator (`run_pipeline`), not
this adapter, is the one that acts on a decline by falling back to another
route; this adapter never inspects or reacts to the report itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._native_route_exec import try_native_route
from decoy_engine.execution.physical._capabilities import CAPABILITIES
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._types import DriverId, ExecutionScope

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._native_route import NativeRouteReport
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._readers import LazySource
    from decoy_engine.relationships import RelationshipGraph


class NativeStreamAdapter:
    """Pure delegation to `try_native_route`. Never treats `(None, report)`
    as an error and never retries or falls back on the caller's behalf."""

    capabilities = CAPABILITIES[DriverId.NATIVE_STREAM]

    def __init__(self) -> None:
        self.last_invocation: SeamContext | None = None

    def run(
        self,
        *,
        config: dict[str, Any],
        plan: Plan,
        table_kinds: dict[str, str],
        caller_sources: Mapping[str, pa.Table | LazySource],
        source_loader: Any,
        sink: TransactionalSink | None,
        fidelity_report: bool,
        execution_mode: str,
        graph: RelationshipGraph,
        resolved_substrate: str = "pandas",
        explain_plan: bool = False,
        execution_plan_decision: ExecutionPlan | None = None,
        batch_rows: int | None = None,
    ) -> tuple[ExecutionResult | None, NativeRouteReport]:
        self.last_invocation = SeamContext(
            driver_id=DriverId.NATIVE_STREAM,
            scope=ExecutionScope.TABLE,
            tables=tuple(table_kinds),
        )
        kwargs: dict[str, Any] = dict(
            config=config,
            plan=plan,
            table_kinds=table_kinds,
            caller_sources=caller_sources,
            source_loader=source_loader,
            sink=sink,
            fidelity_report=fidelity_report,
            execution_mode=execution_mode,
            graph=graph,
            resolved_substrate=resolved_substrate,
            explain_plan=explain_plan,
            execution_plan_decision=execution_plan_decision,
        )
        if batch_rows is not None:
            kwargs["batch_rows"] = batch_rows
        return try_native_route(**kwargs)

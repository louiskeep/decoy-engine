"""`out_of_core` driver adapter (Task 4.2, D2).

Wraps `execution.out_of_core._runner.run_fk_out_of_core`: the whole
relationship-JOB batch-streaming runner. This adapter does not choose the
inner driver (`batch_join` vs `reorder` -- that stays `_route_policy.
decide_route`'s decision, made inside the delegate), does not retain parent
relations itself (that state lives inside the delegate's own run), and does
not split the job into per-table commits -- one commit after all tables,
exactly as production does.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution.out_of_core._runner import run_fk_out_of_core
from decoy_engine.execution.physical._capabilities import CAPABILITIES
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._types import DriverId, ExecutionScope

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._readers import LazySource
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph


class OutOfCoreAdapter:
    """Pure delegation to `run_fk_out_of_core`."""

    capabilities = CAPABILITIES[DriverId.OUT_OF_CORE]

    def __init__(self) -> None:
        self.last_invocation: SeamContext | None = None

    def run(
        self,
        plan: Plan,
        sources: Mapping[str, pa.Table | LazySource],
        *,
        registry: ProviderRegistry,
        relationship_graph: RelationshipGraph,
        sink: TransactionalSink | None = None,
        temp_dir: Path | None = None,
        memory_limit: str | None = None,
        batch_rows: int | None = None,
        budget_bytes: int | None = None,
        temp_disk_budget_bytes: int | None = None,
        unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
        key_provider: KeyProvider | None = None,
        out_of_core_reorder_threshold_rows: int | None = None,
    ) -> ExecutionResult:
        self.last_invocation = SeamContext(
            driver_id=DriverId.OUT_OF_CORE,
            scope=ExecutionScope.RELATIONSHIP_JOB,
            tables=tuple(sources),
        )
        return run_fk_out_of_core(
            plan,
            sources,
            registry=registry,
            relationship_graph=relationship_graph,
            sink=sink,
            temp_dir=temp_dir,
            memory_limit=memory_limit,
            batch_rows=batch_rows,
            budget_bytes=budget_bytes,
            temp_disk_budget_bytes=temp_disk_budget_bytes,
            unconfigured_column_policy=unconfigured_column_policy,
            key_provider=key_provider,
            out_of_core_reorder_threshold_rows=out_of_core_reorder_threshold_rows,
        )

"""`sequential` driver adapter (Task 4.2, D2).

Wraps `execution._sequential.run_sequential` (design doc section 4): the whole
dependency-ordered relationship job, one job-level commit, retained parent
mappings across tables, and BOTH sink shapes production supports (a
`TransactionalSink` -- whole-table `write` + `commit`, `abort()` best-effort on
any exception -- and the legacy immediate/non-transactional plain-callable
sink). This adapter never splits the job into per-table calls (plan C1: that
would change FK-state retention, write order, and commit scope) and never
decides which sink shape is in play -- `run_sequential` itself dispatches on
the sink's type, unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._sequential import run_sequential
from decoy_engine.execution.physical._capabilities import CAPABILITIES
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._types import DriverId, ExecutionScope

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.generation.pool._cache import PoolCache
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


class SequentialAdapter:
    """Pure delegation to `run_sequential`. `adapter` is the pandas
    execution adapter `run_sequential` dispatches every table's strategies
    through -- this class does not construct or select one."""

    capabilities = CAPABILITIES[DriverId.SEQUENTIAL]

    def __init__(self) -> None:
        self.last_invocation: SeamContext | None = None

    def run(
        self,
        adapter: PandasExecutionAdapter,
        plan: Plan,
        source_loader: Callable[[str], pa.Table],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
        sink: TransactionalSink | Callable[[str, pa.Table], None] | None = None,
        quarantine_config: dict[str, Any] | None = None,
        unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
        key_provider: KeyProvider | None = None,
    ) -> ExecutionResult:
        self.last_invocation = SeamContext(
            driver_id=DriverId.SEQUENTIAL,
            scope=ExecutionScope.RELATIONSHIP_JOB,
            tables=(),
        )
        return run_sequential(
            adapter,
            plan,
            source_loader,
            registry=registry,
            pool_cache=pool_cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
            sink=sink,
            quarantine_config=quarantine_config,
            unconfigured_column_policy=unconfigured_column_policy,
            key_provider=key_provider,
        )

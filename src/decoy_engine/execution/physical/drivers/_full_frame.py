"""`full_frame` driver adapter (Task 4.2, D2).

Wraps the SELECTED `ExecutionAdapter` (pandas -- `_pipeline.py`'s
`select_execution_adapter` call, design doc section 4/C3): whole multi-table
mapping, resident output, and a
caller-provided sink is silently ignored because `ExecutionAdapter.run` has no
sink parameter at all -- this adapter does not accept one either, matching the
production shape exactly rather than adding a parameter the delegate would
drop.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution.physical._capabilities import CAPABILITIES
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._types import DriverId, ExecutionScope

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionAdapter, ExecutionResult
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.generation.pool._cache import PoolCache
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


class FullFrameAdapter:
    """Pure delegation to one already-selected `ExecutionAdapter` instance.

    `adapter` is whatever `select_execution_adapter` chose (pandas);
    this class never selects or constructs one itself, matching plan C1
    (drivers do not take over selection/fallback).
    """

    capabilities = CAPABILITIES[DriverId.FULL_FRAME]

    def __init__(self, adapter: ExecutionAdapter) -> None:
        self._adapter = adapter
        self.last_invocation: SeamContext | None = None

    def run(
        self,
        plan: Plan,
        sources: Mapping[str, pa.Table],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
        unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
        generate_output_tables: frozenset[str] = frozenset(),
        key_provider: KeyProvider | None = None,
        row_offset: int = 0,
        code_set_records: Mapping[tuple[str, str], object] | None = None,
    ) -> ExecutionResult:
        self.last_invocation = SeamContext(
            driver_id=DriverId.FULL_FRAME,
            scope=ExecutionScope.FULL_FRAME_JOB,
            tables=tuple(sources),
        )
        return self._adapter.run(
            plan,
            sources,
            registry=registry,
            pool_cache=pool_cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
            unconfigured_column_policy=unconfigured_column_policy,
            generate_output_tables=generate_output_tables,
            key_provider=key_provider,
            row_offset=row_offset,
            code_set_records=code_set_records,
        )

    def supports_strategy(self, strategy_name: str) -> bool:
        return self._adapter.supports_strategy(strategy_name)

    def shutdown(self) -> None:
        self._adapter.shutdown()

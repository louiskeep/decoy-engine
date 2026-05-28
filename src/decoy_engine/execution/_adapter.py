"""ExecutionAdapter protocol + ExecutionResult + strategy-handler contract (S9).

The boundary between planning and execution (S9 spec §2). Concrete adapter at
S9 close: PandasExecutionAdapter. The boundary is Arrow-shaped (`pa.Table` in,
`pa.Table` out); what a strategy does internally (pandas Series ops today,
Polars in S12) is invisible to the boundary.

Refinement vs the spec's StrategyHandler signature: the rarely-used run() deps
(registry, pool_cache, relationship_graph, namespace_registry, job_seed) are
bundled into a frozen `StrategyContext` rather than passed as five separate
kwargs, so a no-backend strategy (passthrough/redact/truncate) does not carry
five unused parameters. Scalar handlers receive one `column: str`; the composite
handler (later slice) writes multiple columns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.instrumentation.timing import StrategyTimingRecord

if TYPE_CHECKING:
    from decoy_engine.generation.pool._cache import PoolCache
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


@dataclass(frozen=True)
class ExecutionResult:
    """The output of `ExecutionAdapter.run(...)` (S9 spec §2).

    `outputs` maps table name -> masked `pa.Table`. A multi-table job (FK
    parent + child masked in one run) carries one entry per table; a
    single-table job carries one. `output` is a convenience accessor for the
    single-table case (it raises rather than guess when the result is
    multi-table; per the slice-2h contract widening, PQ-S9-C).
    """

    outputs: dict[str, pa.Table]
    timings: tuple[StrategyTimingRecord, ...] = ()
    boundary_conversion_ms: float = 0.0
    warnings: tuple[QualityWarning, ...] = ()
    quality_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def output(self) -> pa.Table:
        """The single masked table. Raises if the result holds 0 or >1 tables."""
        if len(self.outputs) != 1:
            raise ExecutionError(
                code="multi_table_result_has_no_single_output",
                message=(
                    f"ExecutionResult holds {len(self.outputs)} tables "
                    f"({sorted(self.outputs)}); use .outputs[table] for a multi-table job."
                ),
            )
        return next(iter(self.outputs.values()))


@dataclass(frozen=True)
class StrategyContext:
    """Shared per-job dependencies threaded into every strategy handler.

    `job_seed` (8 bytes) is the sole entropy input deterministic strategies feed
    into `derive` / `derive_index` / `PoolSampler.sample` (S3 removed per-column
    seed integers).
    """

    registry: ProviderRegistry
    pool_cache: PoolCache
    relationship_graph: RelationshipGraph
    namespace_registry: NamespaceRegistry
    job_seed: bytes


class StrategyHandler(Protocol):
    """A single scalar masking strategy, invoked through the boundary."""

    name: str

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        """Mutate `df[column]` per the plan; return (df, warnings)."""
        ...


class ExecutionAdapter(Protocol):
    """The planning/execution boundary (S9 spec §2). Narrow by design."""

    adapter_name: str
    adapter_version: str

    def run(
        self,
        plan: Plan,
        sources: Mapping[str, pa.Table],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
    ) -> ExecutionResult: ...

    def supports_strategy(self, strategy_name: str) -> bool: ...

    def shutdown(self) -> None: ...


def provider_config_to_dict(provider_config: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """Flatten a `ColumnSeed.provider_config` tuple-of-pairs into a dict."""
    return dict(provider_config)

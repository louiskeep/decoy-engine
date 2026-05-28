"""PandasExecutionAdapter: the first concrete ExecutionAdapter (engine-v2 S9).

Arrow-shaped boundary (`pa.Table` in/out) with a single conversion site each
way (S9 spec §3 + §7). The run loop:

1. Convert source `pa.Table` -> `pd.DataFrame` (timed).
2. Build the work list from `plan.seed_envelope` (NOT FK-only `plan.ordering`).
3. Order it (FK parents before children + R17 composite-before-child).
4. Dispatch each node to its strategy handler.
5. Convert back to `pa.Table` and return `ExecutionResult`.

Slice 2a routes the three no-backend scalar strategies (passthrough, redact,
truncate). Composite nodes + backend-keyed strategies + the Faker/FPE
parallelism land in later slices; a node this slice can't handle raises
ExecutionError(unsupported_strategy) loudly (never a silent pass-through).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult, StrategyContext
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.instrumentation.timing import TimingCollector, timed_strategy, use_collector
from decoy_engine.plan._types import ColumnSeed

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


class PandasExecutionAdapter:
    """Concrete pandas-backed execution adapter."""

    adapter_name: str = "pandas"
    adapter_version: str = pd.__version__

    def __init__(self, *, max_workers: int = 4, fpe_chunk_count: int = 4) -> None:
        self._max_workers = max_workers
        self._fpe_chunk_count = fpe_chunk_count

    def supports_strategy(self, strategy_name: str) -> bool:
        return strategy_name in SCALAR_HANDLERS

    def shutdown(self) -> None:
        """Idempotent resource release. No ThreadPoolExecutors held until the
        parallelism slice; safe to call any time."""
        return None

    def run(
        self,
        plan: Plan,
        source: pa.Table,
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
    ) -> ExecutionResult:
        t0 = time.perf_counter()
        df = source.to_pandas()
        conversion_ms = (time.perf_counter() - t0) * 1000.0

        cache = pool_cache if pool_cache is not None else PoolCache()
        ctx = StrategyContext(
            registry=registry,
            pool_cache=cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
            job_seed=plan.seed_envelope.job_seed,
        )

        ordered = order_work(build_work_list(plan, registry), relationship_graph)

        warnings: list[QualityWarning] = []
        collector = TimingCollector()
        with use_collector(collector):
            for node in ordered:
                if node.kind != "scalar":
                    raise ExecutionError(
                        code="unsupported_strategy",
                        message=(
                            f"node kind {node.kind!r} (columns={node.columns}) is not "
                            "handled yet; composite + backend-keyed routing lands in a "
                            "later S9 slice."
                        ),
                    )
                handler = SCALAR_HANDLERS.get(node.strategy)
                if handler is None:
                    raise ExecutionError(
                        code="unsupported_strategy",
                        message=f"no handler for strategy {node.strategy!r} on {node.columns}.",
                    )
                plan_slice = node.plan_slice
                if not isinstance(plan_slice, ColumnSeed):  # narrows for the scalar handler
                    raise ExecutionError(
                        code="unsupported_strategy",
                        message=f"scalar node {node.columns} has a non-ColumnSeed plan slice.",
                    )
                with timed_strategy(node.strategy, ",".join(node.columns)):
                    df, node_warnings = handler.run(df, node.columns[0], plan_slice, ctx)
                warnings.extend(node_warnings)

        t1 = time.perf_counter()
        output = pa.Table.from_pandas(df, preserve_index=False)
        conversion_ms += (time.perf_counter() - t1) * 1000.0

        return ExecutionResult(
            output=output,
            timings=tuple(collector.records),
            boundary_conversion_ms=conversion_ms,
            warnings=tuple(warnings),
            quality_metrics={},
        )


_DEFAULT_EXECUTOR: PandasExecutionAdapter | None = None


def get_default_executor() -> PandasExecutionAdapter:
    """Return the module-level default pandas executor singleton."""
    global _DEFAULT_EXECUTOR
    if _DEFAULT_EXECUTOR is None:
        _DEFAULT_EXECUTOR = PandasExecutionAdapter()
    return _DEFAULT_EXECUTOR


def _reset_default_executor_for_tests() -> None:
    global _DEFAULT_EXECUTOR
    _DEFAULT_EXECUTOR = None

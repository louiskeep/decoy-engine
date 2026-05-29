"""PolarsExecutionAdapter: the second concrete ExecutionAdapter (engine-v2 S11/S12).

Same `ExecutionAdapter` protocol as S9's pandas adapter; the polars column
substrate sits behind the SAME Arrow boundary (`pa.Table` in, `pa.Table` out).

Dispatch (S12):

- A job whose every node is a polars-native scalar strategy (no FK edges, no
  composite bundles) runs the PURE-POLARS loop: sources convert pa -> pl ONCE at
  ingest, every strategy masks in the polars substrate, and outputs convert
  pl -> pa ONCE at egress (single conversion at the I/O boundary).
- Any other job (an unmigrated strategy, an FK edge, or a composite bundle)
  falls back to the pandas ORACLE wholesale (the S11 behavior): ingest the
  sources into the substrate, round-trip back to Arrow, and let the pandas
  adapter do the masking, returning byte-for-byte identical outputs. FK +
  composite polars migration is a later S12 phase; until then those jobs use the
  oracle.

`_POLARS_NATIVE_STRATEGIES` grows one migration band at a time; a strategy is
native once it is both in that set and in `POLARS_SCALAR_HANDLERS`.

`fallback_to_pandas` (PQ6, PO-ratified 2026-05-28; flip scope dispositioned by
Dennis S55, 2026-05-28): True by default so a polars job completes via the oracle
for any not-yet-native node. FK resolution and composite bundles are NOT
polars-native at V1 ship (FK-loop vectorization is deferred V2+), so they execute
via the oracle on the default substrate. This is NOT a silent downgrade: the
oracle returns byte-for-byte identical output (the pa->pl->pa round-trip is
lossless) AND records the per-strategy substrate of record as "pandas" in
`quality_metrics["executed_substrate"]`.

The S13 polars-default flip changes ONLY `_DEFAULT_SUBSTRATE` -> "polars"; this
flag stays True. The flag is REMOVED (and a non-native job becomes a hard
ExecutionError) only at the post-GA engine release that lands native FK +
composite, NOT at S13 close: removing it earlier would hard-error every FK and
composite job on the default substrate. When `fallback_to_pandas` is EXPLICITLY
set False, a non-native job hard-errors rather than routing through pandas
(cross-sprint contracts non-negotiable on silent downgrades). Pandas is the
parity oracle, not a maintained customer fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

import polars as pl
import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult, StrategyContext
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._guards import reject_null_bearing_int
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.polars._conversion_boundary import ConversionBoundary
from decoy_engine.execution.polars._strategies import POLARS_SCALAR_HANDLERS
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.instrumentation.timing import TimingCollector, timed_strategy, use_collector
from decoy_engine.plan._types import ColumnSeed

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph

# Strategies with a polars-native implementation: exactly the keys of the
# handler registry, so the two cannot drift. Grows per migration band as S12
# adds handlers (cheap band + hash so far).
_POLARS_NATIVE_STRATEGIES: frozenset[str] = frozenset(POLARS_SCALAR_HANDLERS)


class PolarsExecutionAdapter:
    """Concrete polars-substrate execution adapter."""

    adapter_name: str = "polars"
    adapter_version: str = pl.__version__

    def __init__(
        self,
        *,
        max_workers: int = 4,
        fpe_chunk_count: int = 4,
        fallback_to_pandas: bool = True,
    ) -> None:
        # Reserved for a future polars-native per-column parallelism; unused now.
        self._max_workers = max_workers
        self._fallback_to_pandas = fallback_to_pandas
        # The pandas adapter is the parity oracle and the migration-window
        # fallback executor (NOT a maintained customer substrate; see module doc).
        self._pandas = PandasExecutionAdapter(fpe_chunk_count=fpe_chunk_count)
        self._polars_handlers = dict(POLARS_SCALAR_HANDLERS)

    def supports_strategy(self, strategy_name: str) -> bool:
        return strategy_name in _POLARS_NATIVE_STRATEGIES

    def supported_strategies(self) -> frozenset[str]:
        """The set of strategy names this adapter runs polars-native."""
        return _POLARS_NATIVE_STRATEGIES

    def shutdown(self) -> None:
        """Idempotent resource release; delegates to the pandas oracle."""
        self._pandas.shutdown()

    def run(
        self,
        plan: Plan,
        sources: Mapping[str, pa.Table],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
    ) -> ExecutionResult:
        # B1 (S13): reject integer + null-bearing columns under truncate/hash/
        # categorical on the Arrow sources, identically to the pandas adapter, so
        # the polars-native path does not silently accept input the oracle rejects.
        # FK children are exempt (resolved via the edge, not masked).
        reject_null_bearing_int(plan, sources, registry, relationship_graph)
        work = order_work(build_work_list(plan, registry), relationship_graph)
        if self._is_fully_polars_native(work, relationship_graph):
            return self._run_polars_native(
                plan,
                sources,
                work,
                registry=registry,
                pool_cache=pool_cache,
                relationship_graph=relationship_graph,
                namespace_registry=namespace_registry,
            )
        if not self._fallback_to_pandas:
            raise ExecutionError(
                code="polars_substrate_strategy_unmigrated",
                message=(
                    "job has work the polars substrate cannot run natively "
                    f"({self._non_native_reasons(work, relationship_graph)}) "
                    "and fallback_to_pandas is disabled."
                ),
            )
        return self._run_via_pandas_oracle(
            plan,
            sources,
            work,
            registry=registry,
            pool_cache=pool_cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
        )

    def _is_fully_polars_native(
        self, work: list[WorkNode], relationship_graph: RelationshipGraph
    ) -> bool:
        if relationship_graph.edges:
            return False
        return all(
            node.kind == "scalar" and node.strategy in _POLARS_NATIVE_STRATEGIES for node in work
        )

    def _non_native_reasons(
        self, work: list[WorkNode], relationship_graph: RelationshipGraph
    ) -> str:
        reasons: list[str] = []
        if relationship_graph.edges:
            reasons.append("fk_resolution")
        non_native = sorted(
            {
                node.strategy if node.kind == "scalar" else node.kind
                for node in work
                if not (node.kind == "scalar" and node.strategy in _POLARS_NATIVE_STRATEGIES)
            }
        )
        reasons.extend(non_native)
        return ", ".join(reasons) if reasons else "none"

    def _run_polars_native(
        self,
        plan: Plan,
        sources: Mapping[str, pa.Table],
        work: list[WorkNode],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
    ) -> ExecutionResult:
        boundary = ConversionBoundary()
        frames: dict[str, pl.DataFrame] = {
            table: boundary.to_polars(tbl) for table, tbl in sources.items()
        }
        cache = pool_cache if pool_cache is not None else PoolCache()
        ctx = StrategyContext(
            registry=registry,
            pool_cache=cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
            job_seed=plan.seed_envelope.job_seed,
        )
        warnings: list[QualityWarning] = []
        collector = TimingCollector()
        with use_collector(collector):
            for node in work:
                if node.table not in frames:
                    continue
                plan_slice = node.plan_slice
                if not isinstance(plan_slice, ColumnSeed):  # scalar nodes carry a ColumnSeed
                    raise ExecutionError(
                        code="unsupported_strategy",
                        message=f"scalar node {node.columns} has a non-ColumnSeed plan slice.",
                    )
                handler = self._polars_handlers[node.strategy]
                with timed_strategy(node.strategy, ",".join(node.columns)):
                    frames[node.table], node_warnings = handler.run(
                        frames[node.table], node.columns[0], plan_slice, ctx
                    )
                warnings.extend(node_warnings)

        outputs = {table: boundary.to_arrow(frame) for table, frame in frames.items()}
        return ExecutionResult(
            outputs=outputs,
            timings=tuple(collector.records),
            boundary_conversion_ms=boundary.total_ms,
            warnings=tuple(warnings),
            quality_metrics={
                "conversion_breakdown": boundary.as_dict(),
                "executed_substrate": {node.strategy: "polars" for node in work},
            },
        )

    def _run_via_pandas_oracle(
        self,
        plan: Plan,
        sources: Mapping[str, pa.Table],
        work: list[WorkNode],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
    ) -> ExecutionResult:
        # Ingest the sources into the polars substrate and back to Arrow, timing
        # both legs; the masking then runs on the pandas oracle on the
        # round-tripped tables (the pa -> pl -> pa round-trip is lossless, so the
        # result is byte-for-byte identical to a direct pandas run). Used for any
        # job the polars loop cannot yet run natively (unmigrated strategy, FK, or
        # composite).
        boundary = ConversionBoundary()
        substrate_sources: dict[str, pa.Table] = {
            table: boundary.to_arrow(boundary.to_polars(tbl)) for table, tbl in sources.items()
        }
        result = self._pandas.run(
            plan,
            substrate_sources,
            registry=registry,
            pool_cache=pool_cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
        )
        metrics = dict(result.quality_metrics)
        metrics["conversion_breakdown"] = boundary.as_dict()
        # The whole job ran on the pandas oracle in this path; every strategy's
        # substrate of record is "pandas" even if some are individually native.
        metrics["executed_substrate"] = {name: "pandas" for name in self._strategy_names(work)}
        return replace(
            result,
            boundary_conversion_ms=result.boundary_conversion_ms + boundary.total_ms,
            quality_metrics=metrics,
        )

    @staticmethod
    def _strategy_names(work: list[WorkNode]) -> set[str]:
        names: set[str] = set()
        for node in work:
            if node.kind == "scalar":
                names.add(node.strategy)
            elif node.kind == "composite":
                names.add("composite")
        return names


__all__ = ["PolarsExecutionAdapter"]

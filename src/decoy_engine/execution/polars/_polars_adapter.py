"""PolarsExecutionAdapter: the second concrete ExecutionAdapter (engine-v2 S11).

Same `ExecutionAdapter` protocol as S9's pandas adapter; the polars column
substrate sits behind the SAME Arrow boundary (`pa.Table` in, `pa.Table` out).

What S11 ships and what it does NOT:

- S11 ships the I/O boundary (polars-direct `read_source_polars` /
  `write_target_polars`) and this adapter shell. At S11 close NO masking
  strategy is polars-native (`_POLARS_NATIVE_STRATEGIES` is empty); S12 migrates
  the 11 strategies one at a time and populates that set.
- So at S11 close `run(...)` ingests the source tables into the polars substrate
  (pa -> pl, timed), then -- because nothing is polars-native -- falls back to
  the pandas adapter (the parity ORACLE) to do the masking, returning a
  byte-for-byte identical `outputs` dict. S12 inserts per-strategy polars-native
  dispatch between the ingest and egress conversions, shrinking the fallback.

`fallback_to_pandas` is a MIGRATION-WINDOW mechanism only (PQ6, PO-ratified
2026-05-28): True through S12 so a polars job completes by routing unmigrated
strategies through the pandas oracle. At S13 close the flag is REMOVED and an
unmigrated strategy under polars is a hard ExecutionError, not a silent
route-through pandas (cross-sprint contracts non-negotiable on silent
downgrades). Pandas is the parity oracle, not a maintained customer fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

import polars as pl
import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution._runner import build_work_list
from decoy_engine.execution.polars._conversion_boundary import ConversionBoundary

if TYPE_CHECKING:
    from decoy_engine.generation.pool._cache import PoolCache
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


class PolarsExecutionAdapter:
    """Concrete polars-substrate execution adapter (S11: I/O boundary + fallback)."""

    adapter_name: str = "polars"
    adapter_version: str = pl.__version__

    # Strategies with a polars-native implementation. EMPTY at S11 close; S12
    # populates it as each of the 11 strategies migrates.
    _POLARS_NATIVE_STRATEGIES: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        max_workers: int = 4,
        fpe_chunk_count: int = 4,
        fallback_to_pandas: bool = True,
    ) -> None:
        # Reserved for S12 polars-native per-column parallelism; unused at S11.
        self._max_workers = max_workers
        self._fallback_to_pandas = fallback_to_pandas
        # The pandas adapter is the parity oracle and the migration-window
        # fallback executor (NOT a maintained customer substrate; see module doc).
        self._pandas = PandasExecutionAdapter(fpe_chunk_count=fpe_chunk_count)

    def supports_strategy(self, strategy_name: str) -> bool:
        return strategy_name in self._POLARS_NATIVE_STRATEGIES

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
        substrate_map = self._strategy_substrate_map(plan, registry)
        unmigrated = sorted(s for s, sub in substrate_map.items() if sub == "pandas")
        if unmigrated and not self._fallback_to_pandas:
            # The S13-close behavior the flag removal makes permanent: no silent
            # downgrade. At S11/S12 the flag defaults True, so this only fires
            # when a caller explicitly disables fallback before all strategies
            # are polars-native.
            raise ExecutionError(
                code="polars_substrate_strategy_unmigrated",
                message=(
                    f"strategies {unmigrated} have no polars-native implementation "
                    "and fallback_to_pandas is disabled."
                ),
            )

        # Ingest the sources into the polars substrate and back to Arrow, timing
        # both legs. At S11 close nothing is polars-native, so the round-trip is
        # the full extent of the substrate's involvement and the masking falls
        # back to the pandas oracle on the round-tripped tables (parity: the
        # pa->pl->pa round-trip is lossless). S12 replaces the throwaway egress
        # with per-strategy polars-native dispatch.
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
        # Per-strategy substrate of record. At S11 every entry is "pandas" (all
        # strategies fell back); S12 flips entries to "polars" as they migrate.
        metrics["executed_substrate"] = substrate_map
        return replace(
            result,
            boundary_conversion_ms=result.boundary_conversion_ms + boundary.total_ms,
            quality_metrics=metrics,
        )

    def _strategy_substrate_map(self, plan: Plan, registry: ProviderRegistry) -> dict[str, str]:
        """Map each masking strategy in the plan to the substrate that will run it.

        Uses the canonical `build_work_list` enumeration so the classification
        matches what `run` actually dispatches. Structural nodes (FK groups) are
        not masking strategies and are excluded; composite bundles report under
        the single key "composite".
        """
        names: set[str] = set()
        for node in build_work_list(plan, registry):
            if node.kind == "scalar":
                names.add(node.strategy)
            elif node.kind == "composite":
                names.add("composite")
        return {
            name: ("polars" if name in self._POLARS_NATIVE_STRATEGIES else "pandas")
            for name in sorted(names)
        }


__all__ = ["PolarsExecutionAdapter"]

"""`chunked` driver adapter: THREE distinct surfaces (Task 4.2, D2, plan C1/C2).

The chunked driver is not one call shape; production exposes three
independently-characterized entry points and this seam represents all three
separately so it never re-implements the resident aggregation itself:

  * `MaskPipelineChunkedAdapter` wraps `execution._chunked.
    run_mask_pipeline_chunked` -- the lazy `Iterator[pa.Table]` pandas-oracle
    chunk masker.
  * `NativeOrOracleChunkedAdapter` wraps `execution.native._dispatch.
    run_native_or_oracle_chunked` -- also a lazy `Iterator[pa.Table]`, plus
    eager whole-table route admission and mutable route evidence
    (`NativeRouteEvidence`) that fills in as the iterator is consumed.
  * `ResidentChunkedAggregatorAdapter` wraps `execution._pipeline_route_exec.
    run_mask_chunked` -- the RESIDENT aggregator `run_pipeline` actually calls,
    which drains one of the above iterators into the five-part
    `(outputs, timings, boundary_conversion_ms, warnings, quality_metrics)`
    tuple. This adapter does not touch that aggregation logic; it is a bare
    forwarding call.

All three are lazy or resident exactly as production is; none of these
adapters buffers, re-orders, or drains an iterator on the caller's behalf.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._pipeline_route_exec import run_mask_chunked
from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked
from decoy_engine.execution.physical._capabilities import CAPABILITIES
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._types import DriverId, ExecutionScope

if TYPE_CHECKING:
    from decoy_engine.execution.native._dispatch import NativeRouteEvidence
    from decoy_engine.generation.pool._cache import PoolCache
    from decoy_engine.providers_v2 import ProviderRegistry

# `run_mask_pipeline_chunked` lives in `_chunked.py`; imported lazily inside
# the adapter method (not at module scope) purely to mirror the same
# lazy-import discipline `_pipeline_route_exec.run_mask_chunked` itself uses
# for this exact function -- avoids a needless module-level dependency cycle
# risk between `_chunked.py` and this new package.


class MaskPipelineChunkedAdapter:
    """Pure delegation to `run_mask_pipeline_chunked`: lazy `Iterator[pa.Table]`."""

    capabilities = CAPABILITIES[DriverId.CHUNKED]

    def __init__(self) -> None:
        self.last_invocation: SeamContext | None = None

    def run(
        self,
        config: dict[str, Any],
        chunks: Iterable[pa.Table],
        *,
        table: str,
        engine_version: str,
        registry: Any = None,
        adapter: Any = None,
        vault_writer: Any = None,
        chunk_result_sink: list[Any] | None = None,
        key_provider: Any = None,
        base_row_offset: int = 0,
    ) -> Iterator[pa.Table]:
        from decoy_engine.execution._chunked import run_mask_pipeline_chunked

        self.last_invocation = SeamContext(
            driver_id=DriverId.CHUNKED, scope=ExecutionScope.TABLE, tables=(table,)
        )
        return run_mask_pipeline_chunked(
            config,
            chunks,
            table=table,
            engine_version=engine_version,
            registry=registry,
            adapter=adapter,
            vault_writer=vault_writer,
            chunk_result_sink=chunk_result_sink,
            key_provider=key_provider,
            base_row_offset=base_row_offset,
        )


class NativeOrOracleChunkedAdapter:
    """Pure delegation to `run_native_or_oracle_chunked`: lazy
    `Iterator[pa.Table]`, eager whole-table route admission, mutable route
    evidence via `route_evidence_sink`."""

    capabilities = CAPABILITIES[DriverId.CHUNKED]

    def __init__(self) -> None:
        self.last_invocation: SeamContext | None = None

    def run(
        self,
        config: dict[str, Any],
        chunks: Iterable[pa.Table],
        *,
        table: str,
        engine_version: str,
        key_provider: Any = None,
        route_evidence_sink: list[NativeRouteEvidence] | None = None,
        pool_cache: PoolCache | None = None,
        native_threads: int | None = None,
    ) -> Iterator[pa.Table]:
        self.last_invocation = SeamContext(
            driver_id=DriverId.CHUNKED, scope=ExecutionScope.TABLE, tables=(table,)
        )
        return run_native_or_oracle_chunked(
            config,
            chunks,
            table=table,
            engine_version=engine_version,
            key_provider=key_provider,
            route_evidence_sink=route_evidence_sink,
            pool_cache=pool_cache,
            native_threads=native_threads,
        )


class ResidentChunkedAggregatorAdapter:
    """Pure delegation to `_pipeline_route_exec.run_mask_chunked`: the
    RESIDENT aggregator `run_pipeline` calls. Returns the same five-part
    tuple unchanged; never re-drains or re-aggregates an iterator itself."""

    capabilities = CAPABILITIES[DriverId.CHUNKED]

    def __init__(self) -> None:
        self.last_invocation: SeamContext | None = None

    def run(
        self,
        config: dict[str, Any],
        source: pa.Table,
        *,
        table: str,
        engine_version: str,
        registry: ProviderRegistry,
        adapter: Any,
        vault_writer: Any,
        chunk_size_rows: int,
        key_provider: Any = None,
    ) -> tuple[dict[str, pa.Table], tuple[Any, ...], float, tuple[Any, ...], dict[str, Any]]:
        self.last_invocation = SeamContext(
            driver_id=DriverId.CHUNKED, scope=ExecutionScope.TABLE, tables=(table,)
        )
        return run_mask_chunked(
            config,
            source,
            table=table,
            engine_version=engine_version,
            registry=registry,
            adapter=adapter,
            vault_writer=vault_writer,
            chunk_size_rows=chunk_size_rows,
            key_provider=key_provider,
        )

"""The guard-free core of native `run_mask_chunked`.

`run_mask_chunked` refuses a model post-scan config and then returns this core, so the pre-read
sequence (the subset check, the thread check, the declaration gate) and the progress-counted
`_run_chunked` stream live here only. `run_pipeline`'s resident auto-chunk lane runs the post-scan
itself, so it calls this core directly.

The gate and the route helpers are looked up through the `_chunked_entry` module object, so a
patch of `_chunked_entry.prepare_declared_execution_sources` (the declaration-gate seam) holds on
the entry and the lane alike.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution.chunked import observe_chunked
from decoy_engine.execution.native import _chunked_entry
from decoy_engine.fileaccess import FileAccess
from decoy_engine.profile import DeclarationRequest

if TYPE_CHECKING:
    from decoy_engine.execution.native._dispatch import NativeRouteEvidence
    from decoy_engine.generation.pool import PoolCache


def mask_chunked(
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
    native_threads: int = 1,
    route_evidence_sink: list[NativeRouteEvidence] | None = None,
    pool_cache: PoolCache | None = None,
    file_access: FileAccess | None = None,
) -> Iterator[pa.Table]:
    """`run_mask_chunked` without the post-scan route guard."""
    # Looked up at call time, so a patched subset check holds.
    from decoy_engine.config._subset_run import refuse_unapplied_subset

    refuse_unapplied_subset(config)
    _chunked_entry._validate_native_threads(native_threads)
    # Config-level check before `chunks` is touched; the first chunk is checked in preflight.
    declared = _chunked_entry.prepare_declared_execution_sources(
        DeclarationRequest.for_chunks(config, table, None),
        file_access=file_access,
        registry=registry,
    )
    # Progress: the table's mask phase and every yielded chunk on the active channel (bound by
    # the caller, e.g. `run_pipeline(progress=...)`); a no-op when nothing is bound.
    return observe_chunked(
        table,
        _chunked_entry._run_chunked(
            config,
            chunks,
            table=table,
            engine_version=engine_version,
            declared=declared,
            registry=registry,
            adapter=adapter,
            vault_writer=vault_writer,
            chunk_result_sink=chunk_result_sink,
            key_provider=key_provider,
            base_row_offset=base_row_offset,
            native_threads=native_threads,
            route_evidence_sink=route_evidence_sink,
            pool_cache=pool_cache,
        ),
    )

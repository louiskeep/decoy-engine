"""The guard-free core of native `run_mask_chunked`.

`run_mask_chunked` refuses a model post-scan config first, because a direct call runs no
post-scan. `run_pipeline`'s resident auto-chunk lane runs the post-scan itself, so it calls this
core, which does everything the public entry does except that refusal: the subset check, the
thread check, the declaration gate and the progress-counted `_run_chunked` stream.

The gate and the route helpers are looked up through the `_chunked_entry` module object, so a
patch of `_chunked_entry.prepare_declared_sources` (the declaration-gate seam) holds on both paths.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import pyarrow as pa

from decoy_engine.execution.native import _chunked_entry
from decoy_engine.fileaccess import FileAccess
from decoy_engine.progress import get_active_channel


def mask_chunked(
    config: dict[str, Any],
    chunks: Iterable[pa.Table],
    *,
    table: str,
    native_threads: int = 1,
    file_access: FileAccess | None = None,
    **kwargs: Any,
) -> Iterator[pa.Table]:
    """`run_mask_chunked` without the post-scan route guard."""
    # Looked up at call time, as the public entry does, so a patched subset check holds here too.
    from decoy_engine.config._subset_run import refuse_unapplied_subset

    refuse_unapplied_subset(config)
    _chunked_entry._validate_native_threads(native_threads)
    # Config-level check before `chunks` is touched; the first chunk is checked in preflight.
    declared = _chunked_entry.prepare_declared_sources(
        _chunked_entry.DeclarationRequest.for_chunks(config, table, None), file_access=file_access
    )
    # Progress: account every yielded chunk on the active channel (bound by the
    # caller, e.g. `run_pipeline(progress=...)`); a no-op when nothing is bound.
    return get_active_channel().count_rows(
        table,
        _chunked_entry._run_chunked(
            config, chunks, table=table, native_threads=native_threads, declared=declared, **kwargs
        ),
    )

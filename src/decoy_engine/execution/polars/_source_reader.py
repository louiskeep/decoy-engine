"""Polars-direct source.file reader (engine-v2 S11).

S9's pandas path reads a source file via pandas, then converts to `pa.Table` at
the boundary. S11 reads directly into Polars (the substrate decision doc:
"today's source.file reads via pandas then converts to Arrow; tomorrow reads
directly into Polars LazyFrame"), then converts to `pa.Table` ONCE so the rest of
the engine sees the unchanged Arrow boundary. Polars's CSV reader is faster than
pandas's; its Parquet reader is on par.

Scope (S11 Engine-reality note #3): the v2 execution adapter does NOT own the V1
`source.file` graph op. `read_source_polars` is the per-table reader the polars
substrate path calls to populate the `sources: Mapping[str, pa.Table]` dict the
adapter's `run(...)` takes. Wiring the V1 op to this reader is platform/runner
integration, out of S11.

Reader primitives are Polars's `scan_csv` / `scan_parquet` / `scan_ipc` +
`collect` (https://docs.pola.rs), per best-practices section 6.2.
"""

from __future__ import annotations

import time

import polars as pl
import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.polars._conversion_boundary import ConversionBoundary

_SUPPORTED_SOURCE_TYPES = ("csv", "parquet", "ipc")


def read_source_polars(
    path: str,
    *,
    file_type: str,
    boundary: ConversionBoundary | None = None,
) -> pa.Table:
    """Polars-direct read of one source table -> pa.Table at the boundary.

    The caller builds the multi-table `sources` dict by calling this per table.
    When `boundary` is given, the file-read leg accrues to `source_read_ms` and
    the pl->pa conversion to `pl_to_pa_ms`.
    """
    if file_type not in _SUPPORTED_SOURCE_TYPES:
        raise ExecutionError(
            code="unsupported_source_file_type",
            message=(f"source file_type {file_type!r} is not one of {_SUPPORTED_SOURCE_TYPES}."),
        )

    t0 = time.perf_counter()
    if file_type == "csv":
        lazy = pl.scan_csv(path)
    elif file_type == "parquet":
        lazy = pl.scan_parquet(path)
    else:  # ipc
        lazy = pl.scan_ipc(path)
    frame = lazy.collect()
    read_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    table = frame.to_arrow()
    convert_ms = (time.perf_counter() - t1) * 1000.0

    if boundary is not None:
        boundary.source_read_ms += read_ms
        boundary.pl_to_pa_ms += convert_ms
    return table


__all__ = ["read_source_polars"]

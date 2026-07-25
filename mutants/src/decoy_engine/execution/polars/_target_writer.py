"""Polars-direct target.file writer (engine-v2 S11).

Symmetric to `read_source_polars`. Each masked table in
`ExecutionResult.outputs[table]: pa.Table` converts to `pl.DataFrame` and writes
directly via Polars. Called per output table.

Scope mirrors the reader (S11 Engine-reality note #3): this is the engine
function the polars substrate path calls per output table; wiring the V1
`target.file` op to it is platform/runner integration, out of S11.

Writer primitives are Polars's `write_csv` / `write_parquet` / `write_ipc`
(https://docs.pola.rs), per best-practices section 6.2.
"""

from __future__ import annotations

import time

import polars as pl
import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.polars._conversion_boundary import ConversionBoundary

_SUPPORTED_TARGET_TYPES = ("csv", "parquet", "ipc")


def write_target_polars(
    table: pa.Table,
    path: str,
    *,
    file_type: str,
    boundary: ConversionBoundary | None = None,
) -> None:
    """Polars-direct write of one masked table from its `pa.Table`.

    When `boundary` is given, the pa->pl conversion accrues to `pa_to_pl_ms` and
    the file-write leg to `target_write_ms`.
    """
    if file_type not in _SUPPORTED_TARGET_TYPES:
        raise ExecutionError(
            code="unsupported_target_file_type",
            message=(f"target file_type {file_type!r} is not one of {_SUPPORTED_TARGET_TYPES}."),
        )

    t0 = time.perf_counter()
    frame = pl.from_arrow(table)
    convert_ms = (time.perf_counter() - t0) * 1000.0
    if not isinstance(frame, pl.DataFrame):
        raise ExecutionError(
            code="conversion_not_a_frame",
            message=f"pl.from_arrow returned {type(frame).__name__}, expected DataFrame.",
        )

    t1 = time.perf_counter()
    if file_type == "csv":
        frame.write_csv(path)
    elif file_type == "parquet":
        frame.write_parquet(path)
    else:  # ipc
        frame.write_ipc(path)
    write_ms = (time.perf_counter() - t1) * 1000.0

    if boundary is not None:
        boundary.pa_to_pl_ms += convert_ms
        boundary.target_write_ms += write_ms


__all__ = ["write_target_polars"]

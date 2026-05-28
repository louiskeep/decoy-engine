"""Instrumented pa.Table <-> pl.DataFrame conversion boundary (engine-v2 S11).

The polars execution substrate holds table data as `pl.DataFrame` internally and
converts to/from the engine's Arrow boundary (`pa.Table`) at the edges. This
class records the wall-clock cost of the four conversion legs for one job:

    source_read_ms   file -> pl.DataFrame      (the polars-direct reader)
    target_write_ms  pl.DataFrame -> file      (the polars-direct writer)
    pa_to_pl_ms      pa.Table -> pl.DataFrame  (substrate ingest)
    pl_to_pa_ms      pl.DataFrame -> pa.Table  (substrate egress)

The four accumulators roll up into the single
`ExecutionResult.boundary_conversion_ms` float (Engine-reality note #2: the
frozen result grows no new fields). The per-leg breakdown surfaces in
`quality_metrics["conversion_breakdown"]` for the S13 baseline.

Conversion primitives are Polars's own `pl.from_arrow` / `DataFrame.to_arrow`
(https://docs.pola.rs), zero-copy where the Arrow buffer layout allows, per
best-practices section 6.2 (use established methodology, do not roll our own).
"""

from __future__ import annotations

import time

import polars as pl
import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError


class ConversionBoundary:
    """Per-job accumulator for the cost of the four substrate-conversion legs."""

    __slots__ = ("pa_to_pl_ms", "pl_to_pa_ms", "source_read_ms", "target_write_ms")

    def __init__(self) -> None:
        self.source_read_ms = 0.0
        self.target_write_ms = 0.0
        self.pa_to_pl_ms = 0.0
        self.pl_to_pa_ms = 0.0

    def to_polars(self, table: pa.Table) -> pl.DataFrame:
        """pa.Table -> pl.DataFrame (substrate ingest), timed into pa_to_pl_ms."""
        t0 = time.perf_counter()
        frame = pl.from_arrow(table)
        self.pa_to_pl_ms += (time.perf_counter() - t0) * 1000.0
        if not isinstance(frame, pl.DataFrame):
            # pl.from_arrow returns a Series for an Array/ChunkedArray input; a
            # pa.Table always yields a DataFrame. Guard the contract explicitly.
            raise ExecutionError(
                code="conversion_not_a_frame",
                message=f"pl.from_arrow returned {type(frame).__name__}, expected DataFrame.",
            )
        return frame

    def to_arrow(self, frame: pl.DataFrame) -> pa.Table:
        """pl.DataFrame -> pa.Table (substrate egress), timed into pl_to_pa_ms."""
        t0 = time.perf_counter()
        table = frame.to_arrow()
        self.pl_to_pa_ms += (time.perf_counter() - t0) * 1000.0
        return table

    @property
    def total_ms(self) -> float:
        """Sum of the four legs; what folds into boundary_conversion_ms."""
        return self.source_read_ms + self.target_write_ms + self.pa_to_pl_ms + self.pl_to_pa_ms

    def as_dict(self) -> dict[str, float]:
        """The per-leg breakdown for quality_metrics['conversion_breakdown']."""
        return {
            "source_read_ms": self.source_read_ms,
            "target_write_ms": self.target_write_ms,
            "pa_to_pl_ms": self.pa_to_pl_ms,
            "pl_to_pa_ms": self.pl_to_pa_ms,
            "total_ms": self.total_ms,
        }


__all__ = ["ConversionBoundary"]

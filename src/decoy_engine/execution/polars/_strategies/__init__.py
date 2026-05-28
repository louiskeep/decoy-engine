"""Polars-native strategy handlers for the polars execution adapter (S12).

Mirrors the pandas `execution/_strategies/` layout. `POLARS_SCALAR_HANDLERS`
maps a strategy name to its Polars-native handler instance. S12 migrates the 11
mask strategies one band at a time (cheap -> medium -> expensive); a strategy is
polars-native once it appears here AND in
`PolarsExecutionAdapter._POLARS_NATIVE_STRATEGIES`. Until a strategy migrates,
the adapter routes its jobs through the pandas oracle (S11 fallback).

Landed so far (cheap band): passthrough, redact, truncate, shuffle.
"""

from __future__ import annotations

from typing import Protocol

import polars as pl

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution.polars._strategies._passthrough import PolarsPassthroughHandler
from decoy_engine.execution.polars._strategies._redact import PolarsRedactHandler
from decoy_engine.execution.polars._strategies._shuffle import PolarsShuffleStrategyHandler
from decoy_engine.execution.polars._strategies._truncate import PolarsTruncateHandler
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PolarsStrategyHandler(Protocol):
    """A single scalar masking strategy that operates on a pl.DataFrame."""

    name: str

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        """Mask `frame[column]` per the plan; return (frame, warnings)."""
        ...


POLARS_SCALAR_HANDLERS: dict[str, PolarsStrategyHandler] = {
    handler.name: handler
    for handler in (
        PolarsPassthroughHandler(),
        PolarsRedactHandler(),
        PolarsTruncateHandler(),
        PolarsShuffleStrategyHandler(),
    )
}

__all__ = [
    "POLARS_SCALAR_HANDLERS",
    "PolarsPassthroughHandler",
    "PolarsRedactHandler",
    "PolarsShuffleStrategyHandler",
    "PolarsStrategyHandler",
    "PolarsTruncateHandler",
]

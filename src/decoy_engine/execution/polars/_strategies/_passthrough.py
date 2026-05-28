"""passthrough strategy, Polars-native (engine-v2 S12).

Mirrors the pandas `PassthroughHandler` (S9): the column is left unchanged.
"""

from __future__ import annotations

import polars as pl

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PolarsPassthroughHandler:
    """No-op handler: returns the frame unchanged."""

    name: str = "passthrough"

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        return frame, []

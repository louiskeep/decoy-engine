"""passthrough strategy (engine-v2 S9): the column is left unchanged.

Exists for completeness + the dispatch-test surface (S9 spec §4 row 1).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PassthroughHandler:
    """No-op handler: returns the dataframe unchanged."""

    name: str = "passthrough"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        return df, []

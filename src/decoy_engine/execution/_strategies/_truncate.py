"""truncate strategy (engine-v2 S9): keep the first (or last) N characters.

Logic carried from V1 `transforms/truncate.py` (config keys `length` >= 1,
`from_end` bool; nulls preserved; invalid length -> passthrough). No backend.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class TruncateHandler:
    """Keep the first `length` chars of each value (or last, if from_end)."""

    name: str = "truncate"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        length = cfg.get("length")
        if not isinstance(length, int) or length < 1:
            # Invalid config -> passthrough (V1 behavior: one bad rule does not
            # abort the run).
            return df, []
        from_end = bool(cfg.get("from_end", False))
        col = df[column]
        na_mask = col.isna()
        result = col.copy().astype(object)
        non_na = col[~na_mask].astype(str)
        result.loc[~na_mask] = non_na.str[-length:] if from_end else non_na.str[:length]
        df[column] = result
        return df, []

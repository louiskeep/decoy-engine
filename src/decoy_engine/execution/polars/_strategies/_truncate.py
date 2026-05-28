"""truncate strategy, Polars-native (engine-v2 S12).

Mirrors the pandas `TruncateHandler` (S9): keep the first (or last, if
`from_end`) `length` characters of each stringified non-null value; nulls
preserved; an invalid length passes through. The pandas path stringifies via
`astype(str)`; the Polars path casts to Utf8. Output values match for string
sources (the fixtures); the parity harness accepts Arrow-type differences.
"""

from __future__ import annotations

import polars as pl

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PolarsTruncateHandler:
    """Keep the first `length` chars of each value (or last, if from_end)."""

    name: str = "truncate"

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        length = cfg.get("length")
        if not isinstance(length, int) or length < 1:
            # Invalid config -> passthrough (one bad rule does not abort the run).
            return frame, []
        from_end = bool(cfg.get("from_end", False))
        as_str = pl.col(column).cast(pl.Utf8)
        # str.slice keeps nulls null; negative offset takes the trailing window.
        sliced = (as_str.str.slice(-length) if from_end else as_str.str.slice(0, length)).alias(
            column
        )
        return frame.with_columns(sliced), []

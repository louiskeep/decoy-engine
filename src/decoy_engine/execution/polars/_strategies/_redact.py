"""redact strategy, Polars-native (engine-v2 S12).

Mirrors the pandas `RedactHandler` (S9): every non-null value becomes a constant
string (`redact_with`, default "REDACTED"); nulls are preserved. The pandas path
drops to object dtype so the string writes cleanly; the Polars path emits a Utf8
column. Output values are identical; the Arrow type differs (string vs
large_string), which the parity harness accepts as a documented difference.
"""

from __future__ import annotations

import polars as pl

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed

_DEFAULT_REDACT_WITH = "REDACTED"


class PolarsRedactHandler:
    """Replace every non-null value in the column with a fixed string."""

    name: str = "redact"

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        redact_with = cfg.get("redact_with", _DEFAULT_REDACT_WITH)
        replaced = (
            pl.when(pl.col(column).is_null())
            .then(pl.lit(None, dtype=pl.Utf8))
            .otherwise(pl.lit(str(redact_with)))
            .alias(column)
        )
        return frame.with_columns(replaced), []

"""formula strategy (engine-v2 S9): user-defined safe-eval transform.

V1 carryover (S9 spec §4 row 10: "out-of-scope for S9 to expand"). The
safe-eval logic is REUSED from V1 `transforms/formula.FormulaStrategy` rather
than reimplemented (reimplementing a security-sensitive eval is exactly the
risk the reuse rule avoids). No determinism keying: a formula is deterministic
by its expression. The expression comes from `provider_config["formula"]`;
nulls pass through (V1 contract).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.formula import FormulaStrategy

_V1_FORMULA = FormulaStrategy()


class FormulaStrategyHandler:
    """Apply a user-defined expression to each value (V1 safe-eval reused)."""

    name: str = "formula"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        rule = {"formula": cfg.get("formula", ""), "column": column}
        df[column] = _V1_FORMULA.apply(df[column], rule)
        return df, []

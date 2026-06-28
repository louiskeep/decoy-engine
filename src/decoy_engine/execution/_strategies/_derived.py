"""derived strategy handler (engine-v2 SP-10 / P5.S.derived, 2026-06-28).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.derived``.
Core logic (Lark expression evaluation, null propagation, bounds clipping,
column-ref validation) lives in the transforms module for testability and
reuse outside the execution layer.

Config keys accepted via ``plan.provider_config``:
  expression         str   Required. Closed-grammar expression referencing
                           same-table column names as bare identifiers.
  null_propagation   str   Optional. One of "explicit_null" (default),
                           "sentinel", or "default".
  bounds             dict  Optional. {"min": <num>, "max": <num>} clips
                           numeric output.

Mask mode and gen mode: derived is a pure function of the row context
(same input columns -> same output). There is no code branching between
modes; both evaluate the same expression against the row's column values.

Validation timing:
  Expression syntax: parse time (DerivedConfig.from_dict via compile_expr).
  Column-ref existence and cycle detection: plan-compile time (check_derived_
  column_refs in plan/_checks.py), before any execution.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.derived import DerivedConfig, apply_derived


class DerivedStrategyHandler:
    """Compute each row's value from other columns via a closed-grammar expression.

    Delegates to ``decoy_engine.transforms.derived.apply_derived``.
    Config validation (expression syntax) runs at handler invocation via
    DerivedConfig.from_dict, pre-mutation. Column-ref and cycle validation
    runs at plan-compile time via check_derived_column_refs.

    No per-row RNG is used. Determinism is inherent: same row context ->
    same expression output on every run.
    """

    name: str = "derived"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg_dict = provider_config_to_dict(plan.provider_config)
        config = DerivedConfig.from_dict(cfg_dict)

        out: list[object] = []
        for _, row in df.iterrows():
            row_context = row.to_dict()
            out.append(apply_derived(config, row_context))

        df[column] = out
        return df, []

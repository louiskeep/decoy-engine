"""grouped_series strategy handler (SP-10c / P5.S.grouped_series.1).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.grouped_series``.
Core logic (per-group series generation, config validation) lives in the
transforms module for testability and reuse outside the execution layer.

Config keys accepted via ``plan.provider_config``:
  group_by   str  Required. Column name that partitions rows into groups.
  order_by   str  Required. Column name that defines sort order within each
                  group.
  generator  str  Optional. One of "cumcount" (default) or "monotone_walk".
  start      int  Optional. Starting value for each group (default 0 for
                  cumcount, 1 for monotone_walk).
  step       int  Optional. Step between consecutive values (default 1).
  max_step   int  Optional. Upper bound for monotone_walk steps (default 10).

Mask mode: reads group_by + order_by columns from the source DataFrame and
writes the generated series into the target column. The handler receives the
8-byte job seed from ``ctx.job_seed`` for seeded generators (monotone_walk).

Gen mode: see ``generation/synthesize._grouped_series_generate``.

Determinism:
  cumcount: deterministic by group-sorted position; no RNG.
  monotone_walk: deterministic under the same ctx.job_seed.

Validation timing:
  group_by + order_by + generator: config-parse time (GroupedSeriesConfig).
  Column-ref existence: plan-compile time (check_grouped_series_refs).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.grouped_series import GroupedSeriesConfig, apply_grouped_series


class GroupedSeriesStrategyHandler:
    """Generate a per-group series column via the configured generator.

    Delegates to ``decoy_engine.transforms.grouped_series.apply_grouped_series``.
    Config validation (group_by + order_by + generator) runs at handler
    invocation via GroupedSeriesConfig.from_dict, pre-mutation. Column-ref
    existence validation runs at plan-compile time via check_grouped_series_refs.
    """

    name: str = "grouped_series"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg_dict = provider_config_to_dict(plan.provider_config)
        config = GroupedSeriesConfig.from_dict(cfg_dict)
        namespace = f"grouped_series/{column}"
        series = apply_grouped_series(config, df, seed=ctx.mask_key, namespace=namespace)
        df[column] = series.values
        return df, []

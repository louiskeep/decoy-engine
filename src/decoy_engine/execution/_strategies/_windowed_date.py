"""windowed_date strategy handler (SP-10c / P5.S.windowed_date).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.windowed_date``.
Core logic (date arithmetic, distribution sampling, config validation) lives
in the transforms module for testability and reuse outside the execution layer.

Config keys accepted via ``plan.provider_config``:
  anchor        str  Required. Column name containing the anchor date.
  min_days      int  Optional. Minimum offset from anchor in days (default 0).
  max_days      int  Required. Maximum offset from anchor in days.
  distribution  str  Optional. One of "uniform" (default), "early", or "late".

Mask mode: reads the anchor column from the source DataFrame, generates one
output date per row within [anchor + min_days, anchor + max_days], and writes
the result into the target column. The 8-byte job seed from ``ctx.job_seed``
drives per-row offset sampling.

Gen mode: see ``generation/synthesize._windowed_date_generate``.

Determinism:
  Same ctx.job_seed + same anchor column values -> byte-identical output dates.

Validation timing:
  anchor + max_days + distribution: config-parse time (WindowedDateConfig).
  Anchor column existence: plan-compile time (check_windowed_date_refs).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.windowed_date import WindowedDateConfig, apply_windowed_date


class WindowedDateStrategyHandler:
    """Derive a date within a bounded window from an anchor date column.

    Delegates to ``decoy_engine.transforms.windowed_date.apply_windowed_date``.
    Config validation (anchor + max_days + distribution) runs at handler
    invocation via WindowedDateConfig.from_dict, pre-mutation. Anchor column
    existence validation runs at plan-compile time via check_windowed_date_refs.
    """

    name: str = "windowed_date"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg_dict = provider_config_to_dict(plan.provider_config)
        config = WindowedDateConfig.from_dict(cfg_dict)
        namespace = f"windowed_date/{column}"
        date_list = apply_windowed_date(config, df, seed=ctx.mask_key, namespace=namespace)
        df[column] = date_list
        return df, []

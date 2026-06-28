"""joint_mask strategy handler (engine-v2 SP-08, 2026-06-28).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.joint_mask``.
Core logic (HMAC-keyed row selection, gen-mode seeded sampling, config
validation) lives in the transforms module so it can be tested and reused
outside the execution layer.

joint_mask is a MULTI-COLUMN transform: it writes all ``columns`` from the
``joint_masks`` config in a single pass. The handler runs once per joint_mask
group and the ``column`` parameter is the first (alphabetically sorted) column
in the group, used only for adapter-level dispatch tracking.

Config keys accepted via ``plan.provider_config``:
  columns     list[str]  Output columns to write (must match reference table).
  reference   str        Reference table name (e.g. ``us_zip5_city_state``).
  key_by      str        Source column for HMAC-keyed selection (mask mode).
  mode        str        ``"mask"`` (default) or ``"gen"``.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.joint_mask import JointMaskConfig, apply_joint_mask


class JointMaskHandler:
    """Reference-tuple masking: replaces coupled columns with a consistent row.

    Delegates to ``decoy_engine.transforms.joint_mask.apply_joint_mask``.
    Config validation runs at execution time, pre-mutation, fail-closed: an invalid
    ``provider_config`` raises ``PlanCompileError`` (via ``JointMaskConfig.from_dict``)
    before any row is processed.
    """

    name: str = "joint_mask"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        mode = str(cfg.get("mode", "mask"))
        joint_cfg = JointMaskConfig.from_dict(cfg)
        updated_df = apply_joint_mask(df, joint_cfg, mode=mode, job_seed=ctx.job_seed)
        return updated_df, []

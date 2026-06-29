"""group_key strategy handler (SP-10c / P5.P.group_key).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.group_key``.
Core logic (HKDF-SHA256 + HMAC-SHA256 keyed derivation, config validation)
lives in the transforms module for testability and reuse outside the execution
layer.

Config keys accepted via ``plan.provider_config``:
  group_by  str  Required. Column name whose value defines the group.
  length    int  Optional. Output hex-string length (default 16; even, 8-64).
  prefix    str  Optional. Constant prefix prepended to the key (default "").

Mask mode: reads the group_by column from the source DataFrame, derives a
consistent key for each unique group value using the engine's HKDF-SHA256 +
HMAC-SHA256 envelope (same primitive as FK-preserving deterministic masking),
and writes the result into the target column. The namespace for derivation is
"group_key/<target_column_name>" to isolate per-column derivations within the
same job.

Gen mode: see ``generation/synthesize._group_key_generate``.

Determinism:
  Same ctx.job_seed + same namespace + same group_by values -> byte-identical
  key strings on every run (subject to SEED_PROTOCOL_VERSION).

Validation timing:
  group_by + length: config-parse time (GroupKeyConfig.from_dict).
  group_by column existence: plan-compile time (check_group_key_refs).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.group_key import GroupKeyConfig, apply_group_key


class GroupKeyStrategyHandler:
    """Derive a consistent keyed identifier for each group in a column.

    Delegates to ``decoy_engine.transforms.group_key.apply_group_key``.
    Config validation (group_by + length) runs at handler invocation via
    GroupKeyConfig.from_dict, pre-mutation. Column-ref existence validation
    runs at plan-compile time via check_group_key_refs.

    The derivation namespace is "group_key/<column>" so two group_key columns
    in the same job targeting different columns get independent derivation keys.
    """

    name: str = "group_key"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg_dict = provider_config_to_dict(plan.provider_config)
        config = GroupKeyConfig.from_dict(cfg_dict)
        # Namespace isolates this column's derivation from other group_key
        # columns and from mask-path derivations in the same job.
        namespace = f"group_key/{column}"
        key_list = apply_group_key(config, df, seed=ctx.job_seed, namespace=namespace)
        df[column] = key_list
        return df, []

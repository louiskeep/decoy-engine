"""derived_aggregate strategy handler (SP-10b / P5.S.derived_aggregate).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.derived_aggregate``.
Core logic (aggregate computation, config validation) lives in the transforms
module for testability and reuse outside the execution layer.

Config keys accepted via ``plan.provider_config``:
  op      str  Required. One of sum / mean / min / max / count.
  column  str  Required. Source column name in the same table to aggregate.

Mask mode: reads the source column from the DataFrame, computes the aggregate
scalar, and fills every row of the target column with that scalar.

Gen mode (via generation/synthesize._derived_aggregate_generate): reads the
named sibling column from the already-generated snapshot, computes the
aggregate, and fills all rows of the target column.

Determinism: same source column values -> same scalar on every run. No RNG.

Validation timing:
  op + column parse: config-parse time (DerivedAggregateConfig.from_dict).
  column-ref existence: plan-compile time (check_derived_aggregate_refs).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.derived_aggregate import (
    DerivedAggregateConfig,
    apply_derived_aggregate,
)


class DerivedAggregateStrategyHandler:
    """Fill a column with an aggregate scalar from another column in the same row set.

    Delegates to ``decoy_engine.transforms.derived_aggregate.apply_derived_aggregate``.
    Config validation (op + column syntax) runs at handler invocation via
    DerivedAggregateConfig.from_dict, pre-mutation. Column-ref existence
    validation runs at plan-compile time via check_derived_aggregate_refs.

    No per-row RNG is used. Determinism is inherent: same source series ->
    same aggregate on every run.
    """

    name: str = "derived_aggregate"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg_dict = provider_config_to_dict(plan.provider_config)
        config = DerivedAggregateConfig.from_dict(cfg_dict)

        source_series: pd.Series = df[config.column]
        scalar = apply_derived_aggregate(config, source_series)

        # Fill every row of the target column with the aggregate scalar.
        df[column] = scalar
        return df, []

"""geo_generalize strategy handler (engine-v2 SP-08, 2026-06-28).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.geo_generalize``.
Core logic (ZIP cascade, HIPAA Safe Harbor restricted-prefix check, per-row
evidence) lives in the transforms module for testability.

Per-row cascade decisions are surfaced as a ``QualityWarning`` with
code ``"geo_generalize_cascade"`` so the execution result carries an
auditable record of which rows were generalized and to what level.

Config keys accepted via ``plan.provider_config``:
  type         str       ``"zip"`` (only type in SP-08).
  cascade      list[str] Ordered cascade levels ending with ``"suppress"``.
  k_threshold  int       Minimum count to retain a level. Default: 20000.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.geo_generalize import GeoGeneralizeConfig, cascade_zip_column


class GeoGeneralizeHandler:
    """ZIP Safe Harbor cascade: generalise ZIP columns with k-threshold enforcement.

    Delegates to ``decoy_engine.transforms.geo_generalize.cascade_zip_column``.
    Config validation runs at execution time, pre-mutation, fail-closed: an invalid
    ``provider_config`` raises ``PlanCompileError`` before any row is processed.
    Per-row cascade decisions are returned as a ``QualityWarning`` event so
    the execution adapter can surface them in ``ExecutionResult.warnings``.
    """

    name: str = "geo_generalize"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        geo_cfg = GeoGeneralizeConfig.from_dict(cfg)
        updated_df, evidence = cascade_zip_column(df, column, geo_cfg)

        warnings: list[QualityWarning] = []
        if any(d != "zip5" for d in evidence.decisions):
            # Surface cascade decisions as a frozen evidence snapshot.
            # Only emit when at least one row was generalized past zip5.
            decisions_map = {
                f"row_{i}": decision
                for i, decision in enumerate(evidence.decisions)
                if decision != "zip5"
            }
            warnings.append(
                QualityWarning(
                    code="geo_generalize_cascade",
                    provider="geo_generalize",
                    column=column,
                    detail={"cascade_decisions": decisions_map},
                )
            )

        return updated_df, warnings

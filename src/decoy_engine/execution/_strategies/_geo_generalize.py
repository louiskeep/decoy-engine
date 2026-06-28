"""geo_generalize strategy handler (engine-v2 SP-08/SP-08b, 2026-06-28).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.geo_generalize``.
Core logic (ZIP cascade, HIPAA Safe Harbor restricted-prefix check, H3 lat/lng
generalization, per-row evidence) lives in the transforms module for testability.

Per-row cascade decisions are surfaced as a ``QualityWarning`` with
code ``"geo_generalize_cascade"`` so the execution result carries an
auditable record of which rows were generalized and to what level.

Config keys accepted via ``plan.provider_config``:
  type         str       ``"zip"`` (SP-08) or ``"lat_lng"`` (SP-08b, requires [geo] extra).
  cascade      list[str] Ordered cascade levels ending with ``"suppress"``.
  k_threshold  int       Minimum count to retain a level. Default: 20000.

For type="lat_lng", the target column must contain "lat,lng" formatted strings.
Output is the H3 cell index string (not lat/lng coordinates).
Requires the optional [geo] extra (h3 library) when type="lat_lng".
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.geo_generalize import (
    GeoGeneralizeConfig,
    cascade_latlng_column,
    cascade_zip_column,
)


class GeoGeneralizeHandler:
    """Geographic generalization: ZIP Safe Harbor cascade or H3 lat/lng cell snapping.

    Dispatches to the correct transform based on ``config.type``:
      - ``"zip"``     -> ``cascade_zip_column`` (HIPAA Safe Harbor, SP-08).
      - ``"lat_lng"`` -> ``cascade_latlng_column`` (H3 geospatial, SP-08b).

    Config validation runs at execution time, pre-mutation, fail-closed: an invalid
    ``provider_config`` raises ``PlanCompileError`` before any row is processed.
    Per-row cascade decisions are returned as a ``QualityWarning`` event so
    the execution adapter can surface them in ``ExecutionResult.warnings``.

    For ``type="lat_lng"``: requires the optional [geo] extra (h3 library).
    If h3 is absent, fails closed with a clear ImportError naming the extra.
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

        if geo_cfg.type == "lat_lng":
            updated_df, evidence = cascade_latlng_column(df, column, geo_cfg)
            # Derive the top (finest) cascade level from the configured cascade.
            # Hardcoding "h3_resolution_9" mislabels cascades that start coarser
            # (e.g. [h3_resolution_7, h3_resolution_5, suppress]): retained
            # h3_resolution_7 rows would be spuriously flagged in the warning.
            h3_levels = [lvl for lvl in geo_cfg.cascade if lvl != "suppress"]
            non_top_label = h3_levels[0] if h3_levels else "h3_resolution_9"
        else:
            updated_df, evidence = cascade_zip_column(df, column, geo_cfg)
            non_top_label = "zip5"

        warnings: list[QualityWarning] = []
        if any(d != non_top_label for d in evidence.decisions):
            # Surface cascade decisions as a frozen evidence snapshot.
            # Only emit when at least one row was generalized past the highest level.
            decisions_map = {
                f"row_{i}": decision
                for i, decision in enumerate(evidence.decisions)
                if decision != non_top_label
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

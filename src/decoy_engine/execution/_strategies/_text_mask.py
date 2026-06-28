"""text_mask strategy handler (engine-v2 SP-07, 2026-06-28).

Thin V2 StrategyHandler that wraps ``decoy_engine.transforms.text_mask``.
Core logic (HMAC-keyed span masking, dispatch table, unmatched_span_policy)
lives in the transforms module so it can be reused from outside the execution
layer without pulling in the full adapter dependencies.

Config keys accepted via ``plan.provider_config``:
  detectors             list[str] | None  Detector IDs to run. None = all span detectors.
  per_detector_strategy dict[str, str]    Per-detector strategy overrides.
  unmatched_span_policy str               "redact" (default), "passthrough",
                                          "replace_with_token".
  token                 str               Replacement token. Default "[REDACTED]".
  min_days              int               Date-shift lower bound. Default -365.
  max_days              int               Date-shift upper bound. Default 365.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.text_mask import mask_cell


class TextMaskHandler:
    """Span-level PII masking with per-detector strategy dispatch (SP-07).

    Implements the V2 StrategyHandler protocol. Iterates over non-null column
    cells and delegates each to ``mask_cell``, passing ``ctx.job_seed`` as the
    HMAC key for cross-cell determinism.
    """

    name: str = "text_mask"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)

        # Resolve detector list (None = all built-in span detectors).
        detectors_raw = cfg.get("detectors")
        detector_ids: list[str] | None
        if isinstance(detectors_raw, (list, tuple)):
            detector_ids = [str(d) for d in detectors_raw] or None
        else:
            detector_ids = None

        per_detector: dict[str, str] = dict(cfg.get("per_detector_strategy") or {})
        policy = str(cfg.get("unmatched_span_policy", "redact"))
        token = str(cfg.get("token", "[REDACTED]"))

        # Pass date-shift bounds through to mask_cell via the cfg dict.
        extra: dict[str, Any] = {}
        for key in ("min_days", "max_days"):
            if key in cfg:
                extra[key] = cfg[key]

        col = df[column]
        if pd.api.types.is_extension_array_dtype(col.dtype):
            col = col.astype(object)
        else:
            col = col.copy()

        null_mask = col.isna().to_list()
        col_values = col.to_list()

        for pos, value in enumerate(col_values):
            if null_mask[pos]:
                continue
            if not isinstance(value, str):
                value = str(value)
            col_values[pos] = mask_cell(
                value,
                ctx.job_seed,
                detector_ids=detector_ids,
                strategy_map=per_detector or None,
                unmatched_span_policy=policy,
                token=token,
                cfg=extra or None,
            )

        df[column] = pd.Series(col_values, index=df.index, dtype=object)
        return df, []

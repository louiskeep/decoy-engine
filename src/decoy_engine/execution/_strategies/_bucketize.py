"""bucketize strategy (engine-v2 S9): round numeric values into fixed-width bins.

No backend, no determinism keying (deterministic by construction: same value ->
same bucket). Logic carried from V1 `transforms/bucketize.py`: floor(value/width)
* width, formatted per `format` (lower / range / midpoint); width from
`provider_config["width"]` or a `preset` shortcut; non-numeric / NaN fall through
to the original value.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed

_PRESETS: dict[str, int] = {
    "by_year": 1,
    "by_2_years": 2,
    "by_5_years": 5,
    "by_decade": 10,
    "by_century": 100,
    "by_thousand": 1_000,
    "by_ten_thousand": 10_000,
}
_FORMATS = frozenset({"lower", "range", "midpoint"})


class BucketizeStrategyHandler:
    """Round numeric values into fixed-width buckets."""

    name: str = "bucketize"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        width = self._resolve_width(cfg)
        if width is None:
            return df, []  # invalid config -> passthrough (V1 behavior)
        fmt = str(cfg.get("format", "lower")).lower()
        if fmt not in _FORMATS:
            fmt = "lower"

        col = df[column]
        nums = pd.to_numeric(col, errors="coerce")
        lower_f = np.floor(nums / width) * width
        is_int_width = isinstance(width, int) and not isinstance(width, bool)

        if is_int_width:
            lower = lower_f.astype("Int64")
            upper_excl = lower + int(width)
        else:
            lower = lower_f
            upper_excl = lower + width

        if fmt == "lower":
            formatted = lower.astype(str)
        elif fmt == "range":
            upper = upper_excl - 1 if is_int_width else upper_excl
            formatted = lower.astype(str) + "-" + upper.astype(str)
        else:  # midpoint
            mid = lower_f + width / 2
            if is_int_width and int(width) % 2 == 0:
                mid = mid.astype("Int64")
            formatted = mid.astype(str)

        df[column] = formatted.where(nums.notna(), col)
        return df, []

    @staticmethod
    def _resolve_width(cfg: dict[str, Any]) -> int | float | None:
        preset = cfg.get("preset")
        if preset is not None:
            return _PRESETS.get(preset)
        raw = cfg.get("width")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return raw if raw > 0 else None

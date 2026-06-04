"""truncate strategy (engine-v2 S9): keep the first (or last) N characters.

Logic carried from V1 `transforms/truncate.py` (config keys `length` >= 1,
`from_end` bool; nulls preserved; invalid length -> passthrough). No backend.

MG-1 S3 extension (2026-06-01): adds `mask_char` + `keep` so the
V1 "keep last 4, replace rest with *" use case works. When both new
fields are unset, the byte-identical V1 behavior is preserved.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class TruncateHandler:
    """Keep the first `length` chars of each value (or last, if from_end).

    MG-1 S3 (2026-06-01):
      - `keep`: 'head' (default) or 'tail'. Replaces the
        from_end boolean which survives as a deprecation-warned
        synonym for keep='tail'.
      - `mask_char`: when set, the truncated portion is replaced
        with mask_char repeated to fill the dropped span instead
        of being dropped entirely. Output length matches input
        length. When unset, the V1 byte-identical drop behavior
        is preserved.
    """

    name: str = "truncate"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        length = cfg.get("length")
        if not isinstance(length, int) or length < 1:
            # Invalid config -> passthrough (V1 behavior: one bad rule does not
            # abort the run).
            return df, []
        # MG-1/S3: keep + mask_char. Legacy from_end maps to keep="tail";
        # explicit keep wins. mask_char None preserves V1 byte identity.
        from_end_legacy = bool(cfg.get("from_end", False))
        keep = cfg.get("keep")
        if keep is None:
            keep = "tail" if from_end_legacy else "head"
        if keep not in ("head", "tail"):
            return df, []
        mask_char = cfg.get("mask_char")
        if mask_char is not None:
            if not isinstance(mask_char, str) or len(mask_char) != 1:
                return df, []  # rejected at plan-compile, defensive
        col = df[column]
        na_mask = col.isna()
        result = col.copy().astype(object)
        non_na = col[~na_mask].astype(str)
        if mask_char is None:
            # V1 path; byte-identical.
            result.loc[~na_mask] = non_na.str[-length:] if keep == "tail" else non_na.str[:length]
        else:
            # New path: replace truncated portion with mask_char repeated.
            def _mask_one(s: str) -> str:
                if keep == "tail":
                    keep_part = s[-length:]
                    drop_part = s[:-length] if length < len(s) else ""
                    return (mask_char * len(drop_part)) + keep_part
                else:
                    keep_part = s[:length]
                    drop_part = s[length:] if length < len(s) else ""
                    return keep_part + (mask_char * len(drop_part))

            result.loc[~na_mask] = non_na.apply(_mask_one)
        df[column] = result
        return df, []

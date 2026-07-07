"""truncate strategy (engine-v2 S9): keep the first (or last) N characters.

Logic carried from V1 `transforms/truncate.py` (config keys `length` >= 1,
`from_end` bool; nulls preserved). No backend.

MG-1 S3 extension (2026-06-01): adds `mask_char` + `keep` so the
V1 "keep last 4, replace rest with *" use case works. When both new
fields are unset, the byte-identical V1 behavior is preserved.

Sprint 13 / coercion-13 S3 (2026-07-03, finding 0.4): the invalid-config
branches used to `return df, []` (silent passthrough of the source
value -- a masking strategy leaking source PII on a bad config). They now
raise `StrategyError`. `check_truncate_config` (plan/_checks_truncate.py)
rejects the same three shapes at compile time, so this is a defense-in-
depth backstop: unreachable through a compiled plan, but a masking
primitive must never silently emit source values even if some future
caller invokes the handler directly with an unvalidated config. Follows
the `hash_requires_namespace` pattern in `_hash.py`.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.kernel import truncate_array
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
            # Invalid config: fail closed (Sprint 13 finding 0.4). A masking
            # strategy must never silently pass the source value through.
            raise StrategyError(
                code="truncate_length_invalid",
                strategy="truncate",
                message=(
                    f"column {column!r} uses truncate with invalid length "
                    f"{length!r} ({type(length).__name__}); length must be an "
                    "integer >= 1."
                ),
            )
        # MG-1/S3: keep + mask_char. Legacy from_end maps to keep="tail";
        # explicit keep wins. mask_char None preserves V1 byte identity.
        from_end_legacy = bool(cfg.get("from_end", False))
        keep = cfg.get("keep")
        if keep is None:
            keep = "tail" if from_end_legacy else "head"
        if keep not in ("head", "tail"):
            raise StrategyError(
                code="truncate_keep_invalid",
                strategy="truncate",
                message=f"column {column!r} uses truncate with invalid keep {keep!r}.",
            )
        mask_char = cfg.get("mask_char")
        if mask_char is not None:
            if not isinstance(mask_char, str) or len(mask_char) != 1:
                raise StrategyError(
                    code="truncate_mask_char_invalid",
                    strategy="truncate",
                    message=(
                        f"column {column!r} uses truncate with invalid mask_char "
                        f"{mask_char!r} ({type(mask_char).__name__}); mask_char "
                        "must be a single character."
                    ),
                )  # rejected at plan-compile too; this is the defensive backstop
        # SC1 port (2026-07-07): computation now runs through the shared
        # Arrow kernel (`decoy_engine.kernel.truncate_array`) so this handler
        # and the out-of-core route apply byte-identical truncate logic from
        # one source of truth. Byte-identical to the prior inline pandas
        # implementation for every (length, keep, mask_char) combination.
        masked = truncate_array(
            pa.array(df[column], from_pandas=True),
            length=length,
            keep=keep,
            mask_char=mask_char,
        )
        df[column] = masked.to_pylist()
        return df, []

"""bucketize strategy (engine-v2 S9): round numeric values into fixed-width bins.

No backend, no determinism keying (deterministic by construction: same value ->
same bucket). Logic carried from V1 `transforms/bucketize.py`: floor(value/width)
* width, formatted per `format` (lower / range / midpoint); width from
`provider_config["width"]` or a `preset` shortcut; non-numeric / NaN fall through
to the original value (per-VALUE fallback, unrelated to the per-COLUMN config
fallback removed below).

Sprint 13 / coercion-13 S3 (2026-07-03, GATE-1 Q4 sibling of the truncate
fail-closed fix): `_resolve_width` returning `None` used to make `run`
`return df, []` (silent passthrough of the whole column unmasked) on an
unresolved `preset` (including the Studio picker's `"(custom)"` sentinel
reaching the engine unresolved) or a non-numeric/non-positive `width`. It now
raises `StrategyError`. `check_bucketize_config` (plan/_checks_bucketize.py)
rejects the same shapes at compile time; this is the defense-in-depth
backstop. That fix closed the per-COLUMN config-level passthrough.

Sprint 2 honesty pack (2026-07-04, D7/D8): the sibling per-VALUE leak
(referenced in the module docstring above and tracked since #13) is closed
here. A non-null cell that fails `pd.to_numeric` coercion used to keep the
ORIGINAL source value in the output -- silent per-row leak, same class as
the config-level one. `run` now records a `RowError` (trigger
"format_error") on `ctx.row_errors` for such cells via `_row_errors.py`'s
shared channel; it does NOT null or rewrite the cell (trap T4) -- the
pipeline-level rule in `execution/_pipeline.py` (D8) guarantees a row with a
recorded error never reaches the main output (it exits via the quarantine
file, which carries originals by design, or the job fails and nothing is
written). Source-null cells are unaffected (null passthrough is not a leak).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
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
            # Invalid config: fail closed (Sprint 13 GATE-1 Q4). A masking
            # strategy must never silently pass the source column through.
            raise StrategyError(
                code="bucketize_width_unresolvable",
                strategy="bucketize",
                message=(
                    f"column {column!r} uses bucketize but neither a known "
                    f"preset ({cfg.get('preset')!r}) nor a resolvable numeric "
                    f"width ({cfg.get('width')!r}) is configured."
                ),
            )
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

        # Sprint 2 honesty pack (D7): a non-null cell that fails numeric
        # coercion is a per-row format error, NOT a silent keep-original.
        # `col.notna()` isolates non-null source cells; among those, `nums`
        # NaN means coercion failed. Null source cells stay null (unchanged,
        # no error) via `formatted.where(nums.notna(), col)` below, which is
        # unchanged from before -- only the bookkeeping is new.
        non_null_and_uncoercible = col.notna() & nums.isna()
        for i in np.flatnonzero(non_null_and_uncoercible.to_numpy()):
            ctx.row_errors.append(
                RowError(
                    column=column,
                    row_index=int(i),
                    trigger="format_error",
                    reason="value is not numeric under bucketize",
                )
            )
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

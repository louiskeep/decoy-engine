"""date_shift strategy (engine-v2 S9): shift each date by a keyed offset.

Re-keyed onto S3 (S9 spec §4 row 5): the per-value offset in
``[min_days, max_days]`` is `min_days + (int.from_bytes(derive(job_seed,
namespace, _canonicalize_source(value))[:8], "big") % range_size)` -- NOT the
legacy HMAC(column_key)/MD5 path. Same source date -> same shift within a
namespace; byte-stable across runs. Format detection + the vectorized
datetime parse/reformat are reused from V1 `transforms/date_shift._detect_format`.
Null + parse-failed positions restore the original value.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.date_shift import _detect_format


class DateShiftStrategyHandler:
    """Shift dates by a deterministic per-value offset within a bounded range."""

    name: str = "date_shift"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        if plan.namespace is None:
            raise StrategyError(
                code="date_shift_requires_namespace",
                strategy="date_shift",
                message=f"column {column!r} uses date_shift but has no namespace.",
            )
        cfg = provider_config_to_dict(plan.provider_config)
        min_days = int(cfg.get("min_days", -365))
        max_days = int(cfg.get("max_days", 365))
        if min_days > max_days:
            min_days, max_days = max_days, min_days
        range_size = max_days - min_days + 1

        col = df[column]
        if pd.api.types.is_extension_array_dtype(col.dtype):
            col = col.astype(object)
        fmt = cfg.get("date_format") or _detect_format(col)

        parsed = pd.to_datetime(col, format=fmt, errors="coerce")
        unusable = parsed.isna().to_numpy()  # null source OR unparseable date

        shifts: list[int] = []
        for i, value in enumerate(col):
            if unusable[i]:
                shifts.append(0)
                continue
            digest = derive(ctx.job_seed, plan.namespace, _canonicalize_source(value))
            shifts.append(min_days + (int.from_bytes(digest[:8], "big") % range_size))

        shifted = parsed + pd.to_timedelta(shifts, unit="D")
        formatted = shifted.dt.strftime(fmt) if fmt else shifted.astype(str)

        out = [col.iloc[i] if unusable[i] else formatted.iloc[i] for i in range(len(col))]
        df[column] = out
        return df, []

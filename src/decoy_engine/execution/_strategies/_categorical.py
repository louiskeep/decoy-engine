"""categorical strategy (engine-v2 S9): remap values onto a category pool.

Re-keyed onto S3/S5 (S9 spec §4 row 8): the replacement set is a pool of
`provider_config["categories"]`; deterministic mode maps each source value to a
category via `derive_index(job_seed, namespace, _canonicalize_source(value),
pool_size=len(categories))` (same source -> same category within a namespace).
Non-deterministic mode picks uniformly via an UNSEEDED rng -- two non-deterministic
runs differ. This matches the faker + shuffle handlers so "non-deterministic"
means one thing across the strategy set (Dennis slice-2h M2); seed reproducibility
is the deterministic path's job. Null positions preserved.

MG-1 S5 extension (2026-06-01): `weights` and `from_profile`.
- ``cfg["weights"]``: list of floats matching ``categories`` (must be
  same length, non-negative, at least one > 0). Normalized + routed
  through a CDF so picks follow the configured distribution. When
  unset, the uniform path (V1 byte identity) is preserved.
- ``cfg["from_profile"]``: True signals that the plan compiler should
  pull (labels, data) from the column's ``FieldStats.distribution``
  and emit them as ``categories + weights`` on the seed. By the time
  the runtime sees the plan, ``from_profile`` is informational and
  the actual ``categories`` + ``weights`` are already set; the
  plan-compile change lives in ``decoy_engine.plan._compile``.

The deterministic + weighted path uses a CDF over a fixed integer
resolution so ``derive_index(..., pool_size=_WEIGHTED_CDF_RES)`` picks
a uniform integer that maps through the CDF to a weighted category.
Same source value + same namespace + same weights => same category.
"""

from __future__ import annotations

import bisect

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive_index
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


# Resolution for the deterministic-weighted CDF. 1_000_000 supports
# weights down to 1e-6 with the precision the CDF rounding allows.
_WEIGHTED_CDF_RES = 1_000_000


def _build_cdf(weights: list[float]) -> list[int]:
    """Normalize weights + return the CDF as cumulative integer
    thresholds over ``_WEIGHTED_CDF_RES``. The returned list has the
    same length as ``weights``; entry i is the upper-bound (exclusive)
    threshold for category i, so ``bisect_right(cdf, x)`` picks the
    matching index for a uniform ``x`` in ``[0, _WEIGHTED_CDF_RES)``."""
    total = sum(weights)
    if total <= 0:
        raise StrategyError(
            code="categorical_weights_nonpositive",
            strategy="categorical",
            message="categorical weights sum to <= 0; cannot normalize.",
        )
    cdf: list[int] = []
    running = 0.0
    prev_threshold = 0
    for i, w in enumerate(weights):
        if w < 0:
            raise StrategyError(
                code="categorical_weights_negative",
                strategy="categorical",
                message=f"categorical weight {w!r} is negative.",
            )
        running += w
        # Round the cumulative threshold so weights distribute evenly.
        threshold = int(running / total * _WEIGHTED_CDF_RES)
        # QA-3 F9 (2026-05-31): reject weights that round down to a
        # zero-width CDF slot. The bisect_right lookup over a CDF with
        # zero-width slots silently never selects that category, so a
        # weight smaller than 1 / _WEIGHTED_CDF_RES of the total
        # contributed nothing to the output even though the operator
        # asked for it. Fail loud at compile so the operator knows the
        # weight is below the CDF resolution; the alternative -- bump
        # _WEIGHTED_CDF_RES -- breaks determinism for existing plans.
        if w > 0 and threshold == prev_threshold:
            raise StrategyError(
                code="categorical_weight_below_resolution",
                strategy="categorical",
                message=(
                    f"categorical weight {w!r} at index {i} is below the CDF "
                    f"resolution (1 / {_WEIGHTED_CDF_RES} of total). The "
                    "category would never be selected. Either remove the "
                    "category or use weights >= "
                    f"{1.0 / _WEIGHTED_CDF_RES * total:.2e}."
                ),
            )
        prev_threshold = threshold
        cdf.append(threshold)
    # Last entry always lands at the resolution to absorb rounding drift.
    cdf[-1] = _WEIGHTED_CDF_RES
    return cdf


class CategoricalStrategyHandler:
    """Remap a column onto a fixed category pool (derive_index-keyed)."""

    name: str = "categorical"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        categories = list(cfg.get("categories", []))
        if not categories:
            raise StrategyError(
                code="categorical_requires_categories",
                strategy="categorical",
                message=f"column {column!r} uses categorical but provided no categories.",
            )
        # MG-1 S5: optional weights. None = uniform (V1 path).
        weights_raw = cfg.get("weights")
        weights: list[float] | None = None
        if weights_raw is not None:
            if not isinstance(weights_raw, (list, tuple)) or len(weights_raw) != len(categories):
                raise StrategyError(
                    code="categorical_weights_shape",
                    strategy="categorical",
                    message=(
                        f"column {column!r}: weights must be a list with the same "
                        f"length as categories ({len(categories)}); got "
                        f"{type(weights_raw).__name__} "
                        f"len={len(weights_raw) if hasattr(weights_raw, '__len__') else 'n/a'}."
                    ),
                )
            weights = [float(w) for w in weights_raw]

        source = df[column]
        na_mask = source.isna().to_numpy()
        n = len(source)

        if plan.deterministic:
            if plan.namespace is None:
                raise StrategyError(
                    code="categorical_requires_namespace",
                    strategy="categorical",
                    message=f"column {column!r} uses deterministic categorical but has no namespace.",
                )
            out: list[object] = []
            if weights is None:
                # Uniform path -- V1 byte identity.
                for i, value in enumerate(source):
                    if na_mask[i]:
                        out.append(None)
                        continue
                    idx = derive_index(
                        ctx.job_seed,
                        plan.namespace,
                        _canonicalize_source(value),
                        pool_size=len(categories),
                    )
                    out.append(categories[idx])
            else:
                # Weighted path -- CDF over fixed integer resolution.
                cdf = _build_cdf(weights)
                for i, value in enumerate(source):
                    if na_mask[i]:
                        out.append(None)
                        continue
                    bucket = derive_index(
                        ctx.job_seed,
                        plan.namespace,
                        _canonicalize_source(value),
                        pool_size=_WEIGHTED_CDF_RES,
                    )
                    cat_idx = bisect.bisect_right(cdf, bucket)
                    # Defensive: if rounding ever pushes bucket past
                    # cdf[-1] (= _WEIGHTED_CDF_RES), clamp to the
                    # last category.
                    if cat_idx >= len(categories):
                        cat_idx = len(categories) - 1
                    out.append(categories[cat_idx])
        else:
            rng = np.random.default_rng()  # unseeded: non-deterministic contract (M2)
            if weights is None:
                picks = rng.integers(0, len(categories), n)
            else:
                # numpy's choice with p= takes care of normalization.
                # Normalize manually so the StrategyError on sum<=0
                # surfaces before numpy raises.
                total = sum(weights)
                if total <= 0:
                    raise StrategyError(
                        code="categorical_weights_nonpositive",
                        strategy="categorical",
                        message="categorical weights sum to <= 0; cannot normalize.",
                    )
                normalized = [w / total for w in weights]
                picks = rng.choice(len(categories), size=n, p=normalized)
            out = [None if na_mask[i] else categories[int(picks[i])] for i in range(n)]

        df[column] = out
        return df, []

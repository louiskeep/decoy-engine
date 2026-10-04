"""categorical strategy (engine-v2 S9): remap values onto a category pool.

Re-keyed onto S3/S5 (S9 spec §4 row 8): the replacement set is a pool of
`provider_config["categories"]`; deterministic mode maps each source value to a
category via `derive_index(job_seed, namespace, _canonicalize_source(value),
pool_size=len(categories))` (same source -> same category within a namespace).
Non-deterministic mode is reproducible but NOT source-keyed: the draw for the non-null
row at ordinal `g = ctx.row_offset + local_index` is
`derive_index(mask_key, namespace, encode_int(g), pool_size=...)`, so the same job seed
gives the same output and the output does not depend on the source value (no join
preservation). `g` is the ordinal within the frame this handler receives: the physical row
for a plain whole-frame table, the match ordinal under `when:`, the synthetic-frame
ordinal under FK orphan remapping. The key is the public `decoy_engine.kernel.encode_int`,
the same encoding the native batch kernel applies to an integer column, so a later batch
path reproduces these bytes. Null positions preserved (a null still consumes its ordinal).

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

Sprint 13 / coercion-13 S3 (2026-07-03, GATE-1 Q4 sibling of the truncate
fail-closed fix): ``categories = list(cfg.get("categories", []))`` used to
silently iterate the CHARACTERS of a plain-string ``categories`` value
(e.g. a Studio free-text field submitted with no coercion), corrupting
the output to single resampled characters instead of masking with the
intended category set. `run` now raises `StrategyError` when
``categories`` is present and not a list/tuple (unless ``from_profile``
is set, per D5). `check_categorical_categories`
(plan/_checks_categorical.py) rejects the same shape at compile time;
this is the defense-in-depth backstop.

The deterministic + weighted path uses a CDF over a fixed integer
resolution so ``derive_index(..., pool_size=_WEIGHTED_CDF_RES)`` picks
a uniform integer that maps through the CDF to a weighted category.
Same source value + same namespace + same weights => same category.
"""

from __future__ import annotations

import bisect

import pandas as pd

from decoy_engine.determinism import derive_index
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.kernel import encode_int
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
        raw_categories = cfg.get("categories")
        if (
            not cfg.get("from_profile")
            and raw_categories is not None
            and not isinstance(raw_categories, (list, tuple))
        ):
            # Invalid shape: fail closed (Sprint 13 GATE-1 Q4). A string
            # value would iterate as characters below, silently corrupting
            # the output instead of masking with the intended categories.
            raise StrategyError(
                code="categorical_categories_not_list",
                strategy="categorical",
                message=(
                    f"column {column!r} uses categorical with "
                    f"categories={raw_categories!r} ({type(raw_categories).__name__}), "
                    "which is not a list. A string value iterates as individual "
                    "characters at runtime; provide categories as a list."
                ),
            )
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

        if plan.namespace is None:
            mode = "deterministic" if plan.deterministic else "non-deterministic"
            raise StrategyError(
                code="categorical_requires_namespace",
                strategy="categorical",
                message=f"column {column!r} uses {mode} categorical but has no namespace.",
            )
        cdf = _build_cdf(weights) if weights is not None else None
        # Deterministic keys on the canonical source value; non-deterministic keys on
        # the row ordinal in the frame this handler received.
        row_offset = 0 if plan.deterministic else ctx.row_offset
        out: list[object] = []
        for i, value in enumerate(source):
            if na_mask[i]:
                out.append(None)
                continue
            key = _canonicalize_source(value) if plan.deterministic else encode_int(row_offset + i)
            if cdf is None:
                # Uniform path -- V1 byte identity for the deterministic mode.
                out.append(
                    categories[
                        derive_index(ctx.mask_key, plan.namespace, key, pool_size=len(categories))
                    ]
                )
                continue
            bucket = derive_index(ctx.mask_key, plan.namespace, key, pool_size=_WEIGHTED_CDF_RES)
            cat_idx = bisect.bisect_right(cdf, bucket)
            # Defensive: if rounding ever pushes bucket past cdf[-1]
            # (= _WEIGHTED_CDF_RES), clamp to the last category.
            out.append(categories[min(cat_idx, len(categories) - 1)])

        df[column] = out
        return df, []

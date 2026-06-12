"""categorical strategy, Polars-native (engine-v2 S12).

Mirrors the pandas `CategoricalStrategyHandler` (S9 + the MG-1 S5 weights
extension): remap each value onto a fixed category pool. Deterministic mode
maps via `derive_index(job_seed, namespace, _canonicalize_source(value),
pool_size=len(categories))` (shared determinism envelope -> byte-identical
across substrates for a given source value); the deterministic WEIGHTED path
routes a `derive_index(..., pool_size=_WEIGHTED_CDF_RES)` draw through the
same CDF the pandas handler builds (`_build_cdf` is imported from the pandas
module, one source of truth, so the substrates cannot drift).
Non-deterministic mode picks via an UNSEEDED rng (varies per run, by
contract), weighted when `weights` is set. Null positions preserved. Only the
container changes (pl.Series in/out).
"""

from __future__ import annotations

import bisect

import numpy as np
import polars as pl

from decoy_engine.determinism import derive_index
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._categorical import _WEIGHTED_CDF_RES, _build_cdf
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PolarsCategoricalStrategyHandler:
    """Remap a column onto a fixed category pool (derive_index-keyed)."""

    name: str = "categorical"

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        categories = list(cfg.get("categories", []))
        if not categories:
            raise StrategyError(
                code="categorical_requires_categories",
                strategy="categorical",
                message=f"column {column!r} uses categorical but provided no categories.",
            )
        # Weights validation mirrors the pandas handler exactly (same codes).
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

        source = frame[column]
        values = source.to_list()
        na_mask = source.is_null().to_list()
        n = len(values)

        if plan.deterministic:
            if plan.namespace is None:
                raise StrategyError(
                    code="categorical_requires_namespace",
                    strategy="categorical",
                    message=(
                        f"column {column!r} uses deterministic categorical but has no namespace."
                    ),
                )
            out: list[object] = []
            if weights is None:
                for i, value in enumerate(values):
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
                # Weighted path: same CDF as pandas (imported, not copied),
                # same derive_index draw -> byte-identical across substrates.
                cdf = _build_cdf(weights)
                for i, value in enumerate(values):
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
                    if cat_idx >= len(categories):
                        cat_idx = len(categories) - 1
                    out.append(categories[cat_idx])
        else:
            rng = np.random.default_rng()  # unseeded: non-deterministic contract
            if weights is None:
                picks = rng.integers(0, len(categories), n)
            else:
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

        return frame.with_columns(pl.Series(column, out)), []

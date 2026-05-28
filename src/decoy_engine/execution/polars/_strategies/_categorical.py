"""categorical strategy, Polars-native (engine-v2 S12).

Mirrors the pandas `CategoricalStrategyHandler` (S9): remap each value onto a
fixed category pool. Deterministic mode maps via `derive_index(job_seed,
namespace, _canonicalize_source(value), pool_size=len(categories))` (shared
determinism envelope -> byte-identical across substrates for a given source
value); non-deterministic mode picks uniformly via an UNSEEDED rng (varies per
run, by contract). Null positions preserved. Only the container changes
(pl.Series in/out).
"""

from __future__ import annotations

import numpy as np
import polars as pl

from decoy_engine.determinism import derive_index
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
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
            rng = np.random.default_rng()  # unseeded: non-deterministic contract
            picks = rng.integers(0, len(categories), n)
            out = [None if na_mask[i] else categories[int(picks[i])] for i in range(n)]

        return frame.with_columns(pl.Series(column, out)), []

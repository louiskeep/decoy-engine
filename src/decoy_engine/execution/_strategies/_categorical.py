"""categorical strategy (engine-v2 S9): remap values onto a category pool.

Re-keyed onto S3/S5 (S9 spec §4 row 8): the replacement set is a pool of
`provider_config["categories"]`; deterministic mode maps each source value to a
category via `derive_index(job_seed, namespace, _canonicalize_source(value),
pool_size=len(categories))` (same source -> same category within a namespace).
Non-deterministic mode picks uniformly via a seeded rng. Null positions
preserved.

(V1's weighted / null_probability sampling is not carried into the S9 baseline;
the S9 contract is the derive_index uniform remap. Weighted remap is a V2+
refinement.)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive_index
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


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
            rng = np.random.default_rng(int.from_bytes(ctx.job_seed, "big"))
            picks = rng.integers(0, len(categories), n)
            out = [None if na_mask[i] else categories[int(picks[i])] for i in range(n)]

        df[column] = out
        return df, []

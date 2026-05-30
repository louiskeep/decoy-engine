"""shuffle strategy (engine-v2 S9): within-column permutation, multiset-preserving.

Re-keyed onto S3 (S9 spec §4 row 9 / R19): the permutation rng is seeded from
`derive(job_seed, namespace, b"")` (column-stable, NOT the global
`np.random.seed`), so a deterministic shuffle is byte-stable across runs.
Non-deterministic mode uses an unseeded `default_rng`. Null positions are
preserved; only the non-null values are permuted.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class ShuffleStrategyHandler:
    """Permute the non-null values of a column (multiset + nulls preserved)."""

    name: str = "shuffle"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        source = df[column]
        na_mask = source.isna().to_numpy()
        non_na_positions = np.where(~na_mask)[0]
        non_na_values = source.to_numpy(dtype=object)[~na_mask]

        if plan.deterministic:
            if plan.namespace is None:
                raise StrategyError(
                    code="shuffle_requires_namespace",
                    strategy="shuffle",
                    message=f"column {column!r} uses deterministic shuffle but has no namespace.",
                )
            seed_int = int.from_bytes(derive(ctx.job_seed, plan.namespace, b"")[:8], "big")
            rng = np.random.default_rng(seed_int)
        else:
            rng = np.random.default_rng()

        permuted = non_na_values[rng.permutation(len(non_na_values))]
        out: list[object] = [None] * len(source)
        for offset, position in enumerate(non_na_positions):
            out[int(position)] = permuted[offset]
        # Q13 fix (S11 gate, 2026-05-30): wrap the list[object] in an explicit
        # object-dtype Series so the column assignment does not let pandas
        # re-infer float64 from int + None mixes (the V1 anti-pattern QA Q13
        # surfaced and the rev 2 plan's "verified-fixed" triage missed).
        df[column] = pd.Series(out, dtype=object, index=df.index)
        return df, []

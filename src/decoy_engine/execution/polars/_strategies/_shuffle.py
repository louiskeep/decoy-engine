"""shuffle strategy, Polars-native (engine-v2 S12).

Mirrors the pandas `ShuffleStrategyHandler` (S9): a within-column,
multiset-preserving permutation of the non-null values; null positions
preserved. The permutation RNG is the SAME shared primitive as the pandas path
(`numpy.random.default_rng` seeded from `derive(job_seed, namespace, b"")` for
deterministic mode), so for a given seed the permutation is byte-identical across
substrates. Only the data container changes (pl.Series in/out); the permutation
logic is not reimplemented per substrate (substrate-decision doc: shared
primitive, container-only migration).
"""

from __future__ import annotations

import numpy as np
import polars as pl

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PolarsShuffleStrategyHandler:
    """Permute the non-null values of a column (multiset + nulls preserved)."""

    name: str = "shuffle"

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        source = frame[column]
        values = source.to_list()
        na_mask = source.is_null().to_list()
        non_na_positions = [i for i, is_null in enumerate(na_mask) if not is_null]
        non_na_values = [values[i] for i in non_na_positions]

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

        permutation = rng.permutation(len(non_na_values))
        out: list[object] = [None] * len(values)
        for offset, position in enumerate(non_na_positions):
            out[position] = non_na_values[int(permutation[offset])]
        return frame.with_columns(pl.Series(column, out, dtype=source.dtype)), []

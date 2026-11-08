"""shuffle strategy (engine-v2 S9): within-column permutation, multiset-preserving.

Re-keyed onto S3 (S9 spec §4 row 9 / R19): the permutation rng is seeded from
`derive(job_seed, namespace, column_name)` (column-stable, NOT the global
`np.random.seed`), so a deterministic shuffle is byte-stable across runs.
The column name is bound into the derivation source so two shuffle columns
sharing a namespace draw distinct permutations; otherwise they would be
permuted in lockstep and re-link the values masking is meant to decouple.
Non-deterministic mode is unkeyed (no namespace) but still drawn from the mask
key, with the job seed and the shared length-prefixed (table, column) identity
(`context_draw_identity`, the one non-deterministic Faker keys on) bound in, so the
same config, seed and secret reproduce the output. Null positions are preserved;
only the non-null values are permuted.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine import reasons
from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import (
    StrategyContext,
    StrategyOption,
    context_draw_identity,
    option,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed

# Derivation namespace of the unkeyed mode, distinct from any configured namespace.
UNKEYED_SHUFFLE_NAMESPACE = "decoy.shuffle.unkeyed/v1"


class ShuffleStrategyHandler:
    """Permute the non-null values of a column (multiset + nulls preserved)."""

    name: str = "shuffle"
    namespace_required: bool = True
    join_safe: bool = False
    wizard_options: tuple[StrategyOption, ...] = (
        option("deterministic", "bool", True, location="column"),
    )
    offer_in_wizard: bool = True

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
                    code=reasons.runtime.SHUFFLE_REQUIRES_NAMESPACE,
                    strategy="shuffle",
                    message=f"column {column!r} uses deterministic shuffle but has no namespace.",
                )
            namespace, draw_source = plan.namespace, column
        else:
            # The (table, outer column) identity keeps two unkeyed shuffles out of lockstep (a
            # nested child is always called `_nested_leaves`); the job seed makes a new seed
            # draw a new permutation. Its hex has no `/`, so the join cannot be ambiguous.
            identity = context_draw_identity(ctx, column, strategy="shuffle")
            namespace = UNKEYED_SHUFFLE_NAMESPACE
            draw_source = f"{ctx.job_seed.hex()}/{identity}"
        seed_int = int.from_bytes(
            derive(ctx.mask_key, namespace, draw_source.encode("utf-8"))[:8], "big"
        )
        rng = np.random.default_rng(seed_int)

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

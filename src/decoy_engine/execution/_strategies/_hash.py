"""hash strategy (engine-v2 S9): joinability-preserving deterministic token.

Re-keyed onto S3 (S9 spec §8 path #1): the masked token is
`derive(job_seed, namespace, _canonicalize_source(value)).hex()`, optionally
truncated. Same source value -> same token within a namespace (joinability
preserved), byte-stable across runs/processes. NOT the legacy
`hmac_hex(column_key, ...)` / `deterministic_hash(s, seed:int)` path.

The canonical source bytes come from S5's `_canonicalize_source` (R3); the
strategy does not do its own `.encode`. A namespace is required (the token is
keyed on it); a hash column without one is a wiring error.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import (
    StrategyContext,
    pandas_column_to_kernel_input,
    provider_config_to_dict,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.kernel import hash_array
from decoy_engine.plan._types import ColumnSeed


class HashStrategyHandler:
    """Deterministic joinability-preserving hash via derive(...)."""

    name: str = "hash"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        if plan.namespace is None:
            raise StrategyError(
                code="hash_requires_namespace",
                strategy="hash",
                message=f"column {column!r} uses the hash strategy but has no namespace.",
            )
        cfg = provider_config_to_dict(plan.provider_config)
        raw_truncate = cfg.get("truncate")
        truncate = raw_truncate if isinstance(raw_truncate, int) and raw_truncate > 0 else None

        masked = hash_array(
            pandas_column_to_kernel_input(df[column]),
            seed=ctx.job_seed,
            namespace=plan.namespace,
            truncate=truncate,
            derive_func=derive,
        )
        df[column] = masked.to_pylist()
        return df, []

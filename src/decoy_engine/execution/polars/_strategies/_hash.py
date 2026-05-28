"""hash strategy, Polars-native (engine-v2 S12).

Mirrors the pandas `HashStrategyHandler` (S9): a joinability-preserving
deterministic token, `derive(job_seed, namespace, _canonicalize_source(value)).hex()`,
optionally truncated; nulls preserved. The keyed primitive (`derive`) and the
canonicalization (`_canonicalize_source`, which dispatches by type and coerces
python and numpy integers identically) are the SHARED determinism envelope, not
reimplemented per substrate, so the token is byte-identical across substrates for
a given source value. Only the data container changes (pl.Series in/out).
"""

from __future__ import annotations

import polars as pl

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PolarsHashStrategyHandler:
    """Deterministic joinability-preserving hash via derive(...)."""

    name: str = "hash"

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        if plan.namespace is None:
            raise StrategyError(
                code="hash_requires_namespace",
                strategy="hash",
                message=f"column {column!r} uses the hash strategy but has no namespace.",
            )
        cfg = provider_config_to_dict(plan.provider_config)
        raw_truncate = cfg.get("truncate")
        truncate = raw_truncate if isinstance(raw_truncate, int) and raw_truncate > 0 else None

        source = frame[column]
        values = source.to_list()
        out: list[str | None] = []
        for value in values:
            if value is None:
                out.append(None)
                continue
            token = derive(ctx.job_seed, plan.namespace, _canonicalize_source(value)).hex()
            out.append(token[:truncate] if truncate is not None else token)
        return frame.with_columns(pl.Series(column, out, dtype=pl.Utf8)), []

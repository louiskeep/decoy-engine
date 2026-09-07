"""hash strategy, Polars-native (engine-v2 S12).

Mirrors the pandas `HashStrategyHandler` (S9): a joinability-preserving
deterministic token, `derive(job_seed, namespace, _canonicalize_source(value)).hex()`,
optionally truncated; nulls preserved. The keyed primitive (`derive`) and the
canonicalization (`_canonicalize_source`, which dispatches by type and coerces
python and numpy integers identically) are the SHARED determinism envelope, not
reimplemented per substrate, so the token is byte-identical across substrates for
a given source value.

Routes through the shared `hash_array` kernel via `source.to_arrow()` rather than
`pl.Series.to_list()`: `to_list()` on a sub-microsecond timestamp yields a
us-truncated stdlib `datetime` (Python's own resolution ceiling), while
`to_arrow()` -> `pa.Array.to_pylist()` yields an ns-preserving `pandas.Timestamp`,
matching the pandas adapter's `pa.array(col, from_pandas=True).to_pylist()` byte
for byte (2026-09-02 polars-hash-kernel-parity plan). Unconditional -- no dtype
predicate -- because the Polars adapter's ingestion step already corrupts the
exotic dtypes (date64, negative-scale decimal, fixed-offset tz) before this
handler ever runs, so a handler-level fallback could not recover them anyway;
those stay refused as FK keys upstream (the cascade plan's dtype restriction).
Only the data container changes (pl.Series in/out).
"""

from __future__ import annotations

import polars as pl

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.kernel import hash_array
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
        masked = hash_array(
            source.to_arrow(),
            seed=ctx.mask_key,
            namespace=plan.namespace,
            truncate=truncate,
            derive_func=derive,
        )
        return frame.with_columns(pl.Series(column, masked.to_pylist(), dtype=pl.Utf8)), []

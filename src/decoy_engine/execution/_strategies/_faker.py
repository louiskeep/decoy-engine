"""faker strategy (engine-v2 S9): pool-backed value generation.

Re-keyed onto S5 (NOT the legacy V1 derive_key/seed:int path). Determinism is
the pool path (S9 spec §8 path #2): build/fetch a `ValuePool` for the provider
via `PoolBuilder`, then call the VECTORIZED `PoolSampler.sample(...)` ONCE for
the whole column. The sampler's deterministic branch does the per-row
`derive_index(job_seed, namespace, _canonicalize_source(src), pool_size)` with
null preservation internally; non-deterministic mode uses `default_rng(seed)`.
Calling `PoolSampler.sample` once (not `PoolAdapter.generate` per row) is what
keeps the >=10x Faker performance gate reachable.

Source nulls are preserved in both modes (the sampler preserves them in
deterministic mode; this handler restores them in non-deterministic mode too,
for a uniform null contract).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool import CardinalityMode, PoolBuilder, PoolSampler, ValuePool
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed

_DEFAULT_POOL_SIZE = 10_000


class FakerStrategyHandler:
    """Pool-backed masking via PoolBuilder + the vectorized PoolSampler."""

    name: str = "faker"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        if plan.provider is None:
            # A faker strategy without a provider is an invalid plan that
            # validation should have rejected; guard so the type is concrete
            # and the failure is named rather than a None reaching PoolBuilder.
            raise ValueError(f"faker strategy on column {column!r} has no provider")
        source = df[column]
        n = len(source)
        cfg = provider_config_to_dict(plan.provider_config)
        pool_size = int(cfg.get("pool_size", _DEFAULT_POOL_SIZE))
        locale = cfg.get("locale")
        # pool_size + locale are build knobs, not Faker provider-method kwargs.
        build_config = {k: v for k, v in cfg.items() if k not in ("pool_size", "locale")}

        # Consult ctx.pool_cache before building. Safe for byte parity:
        # the build is RNG-seeded by the identity's pool_seed (S5 F2), so
        # a cached pool and a rebuilt pool of the same identity are
        # value-identical (S5 F1 established the identity_for cheap
        # lookup for exactly this reason). Chunked execution pre-warms
        # the cache so every chunk reuses one pool instead of rebuilding.
        builder = PoolBuilder(ctx.registry)
        identity = builder.identity_for(
            plan.provider,
            size=pool_size,
            job_seed=ctx.job_seed,
            locale=locale,
            config=build_config,
            namespace=plan.namespace,
        )
        cached = ctx.pool_cache.get(identity)
        pool = cached if isinstance(cached, ValuePool) else None
        if pool is None:
            pool = builder.build(
                provider=plan.provider,
                size=pool_size,
                job_seed=ctx.job_seed,
                locale=locale,
                config=build_config,
                namespace=plan.namespace,
            )
            ctx.pool_cache.put(pool)
        sampled = PoolSampler().sample(
            pool,
            n,
            mode=CardinalityMode(plan.cardinality_mode),
            seed=ctx.job_seed,
            source=source,
            namespace=plan.namespace,
            deterministic=plan.deterministic,
        )

        na_mask = source.isna().to_numpy()
        values = list(sampled)
        df[column] = [None if na_mask[i] else values[i] for i in range(n)]
        return df, []

"""faker strategy (engine-v2 S9): pool-backed value generation.

Re-keyed onto S5 (NOT the legacy V1 derive_key/seed:int path). Determinism is
the pool path (S9 spec §8 path #2): build/fetch a `ValuePool` for the provider
via `PoolBuilder`, then select from it. Three selections exist:

- Deterministic: the VECTORIZED `PoolSampler.sample(...)` called ONCE for the whole
  column. Its deterministic branch does the per-row
  `derive_index(mask_key, namespace, _canonicalize_source(src), pool_size)` with null
  preservation internally. Calling it once (not `PoolAdapter.generate` per row) is what
  keeps the >=10x Faker performance gate reachable.
- Non-deterministic REUSE: position-keyed on `job_seed` (`_faker_positional`). Row `g`
  draws `pool.values[derive_index(job_seed, selection_namespace, encode_int(g), size)]`,
  where `g` is the row's ordinal in the frame this handler receives plus `ctx.row_offset`.
  The source value is ignored.
- Non-deterministic UNIQUE / MATCH / SCALE: `PoolSampler.sample` with `default_rng` off
  `job_seed`. These need whole-column state, so they stay one numpy stream per column.

The pool is built identically in every mode (the selection namespace never reaches the
build). Source nulls are preserved in all of them.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._exact_int_faker import sampling_source
from decoy_engine.execution._strategies._faker_positional import (
    positional_pool_indices,
    resolve_selection_namespace,
)
from decoy_engine.generation.pool import CardinalityMode, PoolBuilder, PoolSampler, ValuePool
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.generation.pool._identity import DEFAULT_POOL_SCALE, resolve_faker_pool_identity
from decoy_engine.plan._types import ColumnSeed


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
        scale = plan.scale if plan.scale is not None else DEFAULT_POOL_SCALE
        mode = CardinalityMode(plan.cardinality_mode)
        positional = not plan.deterministic and mode is CardinalityMode.REUSE
        # Fail before the pool build when the draw cannot be keyed.
        selection_namespace = (
            resolve_selection_namespace(ctx, column, plan.namespace) if positional else ""
        )

        # Consult ctx.pool_cache before building. Safe for byte parity:
        # the build is RNG-seeded by the identity's pool_seed (S5 F2), so
        # a cached pool and a rebuilt pool of the same identity are
        # value-identical (S5 F1 established the identity_for cheap
        # lookup for exactly this reason). Chunked execution pre-warms
        # the cache so every chunk reuses one pool instead of rebuilding.
        # `resolve_faker_pool_identity` (Phase 3 Task 3.1 HIGH 1) is the ONE
        # place this pool_size/locale/build_config split lives, shared with
        # the native chunked route so the two sides cannot compute different
        # identities for the same column.
        builder = PoolBuilder(ctx.registry)
        pool_size, locale, build_config, identity = resolve_faker_pool_identity(
            builder=builder,
            provider=plan.provider,
            plan_pool_size=plan.pool_size,
            namespace=plan.namespace,
            job_seed=ctx.job_seed,
            cfg=cfg,
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
        na_mask = source.isna().to_numpy()
        if positional:
            # Job-seed keyed: non-deterministic mode generates fresh synthetic values and
            # never re-identifies a source value, so it stays off the secret-derived key.
            idx = positional_pool_indices(
                n,
                row_offset=ctx.row_offset,
                job_seed=ctx.job_seed,
                namespace=selection_namespace,
                pool_size=pool.size,
                gate_positions=ctx.gate_positions,
            )
            chosen = pool.values[idx]
            df[column] = [None if na_mask[i] else chosen[i] for i in range(n)]
            return df, []

        # DE-02 seam: pool BUILD stays on job_seed (fresh synthetic values); only
        # the deterministic SELECTION from a real source value re-keys onto
        # mask_key. The whole-column non-deterministic modes ignore `source` values
        # for the draw and generate off job_seed (generation, not a re-identification
        # surface).
        select_seed = ctx.mask_key if plan.deterministic else ctx.job_seed
        sampled = PoolSampler().sample(
            pool,
            n,
            mode=mode,
            seed=select_seed,
            source=sampling_source(ctx, column, source, deterministic=plan.deterministic),
            namespace=plan.namespace,
            deterministic=plan.deterministic,
            scale=scale,
        )

        values = list(sampled)
        df[column] = [None if na_mask[i] else values[i] for i in range(n)]
        return df, []

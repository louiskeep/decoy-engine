"""Shared faker pool-identity resolution (Phase 3 Task 3.1, HIGH 1).

`FakerStrategyHandler.run` (the pandas oracle) and the native chunked route
(`execution.native._dispatch`) must build byte-identical pools for the same
column: two independently-typed pool_size/locale/build_config splits would
silently diverge the two routes onto different cache identities, and thus
different sampled values. This module is the ONE place that split lives, so
oracle and native can never drift apart on it.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.generation.pool._builder import PoolBuilder
from decoy_engine.generation.pool._runtime_pool_size import resolve_runtime_pool_size

# Faker's per-column scale factor, used only under SCALE_SOURCE_CARDINALITY;
# inert under REUSE (the sole C1-scoped mode). Shared so both call sites
# resolve an unset `scale` to the same default.
DEFAULT_POOL_SCALE = 2.0

PoolIdentity = tuple[str, str, str, bytes, int]


def resolve_faker_pool_identity(
    *,
    builder: PoolBuilder,
    provider: str,
    plan_pool_size: int | None,
    namespace: str | None,
    job_seed: bytes,
    cfg: dict[str, Any],
) -> tuple[int, str | None, dict[str, Any], PoolIdentity]:
    """Resolve `(pool_size, locale, build_config, identity)` for one faker column.

    Mirrors `FakerStrategyHandler.run`'s prior inline computation exactly:
    `pool_size` prefers the compiled `ColumnSeed.pool_size` (DE-11), falling
    back to a raw-config read only for a hand-built `ColumnSeed` that bypassed
    `compile_plan`; `locale` and `build_config` split the same way, since
    `pool_size`/`locale` are pool-BUILD knobs, not Faker provider-method
    kwargs, and must not leak into the config hash the other kwargs feed.
    """
    pool_size = plan_pool_size if plan_pool_size is not None else resolve_runtime_pool_size(cfg)
    locale = cfg.get("locale")
    build_config = {k: v for k, v in cfg.items() if k not in ("pool_size", "locale")}
    identity = builder.identity_for(
        provider,
        size=pool_size,
        job_seed=job_seed,
        locale=locale,
        config=build_config,
        namespace=namespace,
    )
    return pool_size, locale, build_config, identity


__all__ = ["DEFAULT_POOL_SCALE", "PoolIdentity", "resolve_faker_pool_identity"]

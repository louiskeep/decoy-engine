"""Shared pool-capacity arithmetic (DE-11).

ONE definition of "is this pool big enough for a UNIQUE draw", plus ONE
resolver for the canonical `pool_size` config location. Both are imported by
the compile-time feasibility check (`_validate`), the seed-envelope builder
(`plan._seed_envelope`), the runtime sampler (`_sampler`), and the faker
handler (`execution._strategies._faker`), so those layers can never disagree
on how large the pool is or on whether a UNIQUE draw fits.

Root cause of DE-11 (two independent defects that this module closes):

1. Two disagreeing capacity checks. Compile sized UNIQUE feasibility against
   the SOURCE distinct count (`pool_size >= source.distinct_count`); the
   sampler sized it against the number of rows it fills. Those are different
   quantities, so a job could pass compile and then behave differently at
   run time. The correct quantity for UNIQUE is the NON-NULL OUTPUT ROW COUNT:
   the sampler must place one distinct value in every non-null output row, and
   the source distinct count is irrelevant to that.

2. Two config locations. Compile read the top-level column `pool_size`; the
   faker handler read `provider_config['pool_size']`, defaulting to 10_000. A
   top-level `pool_size` therefore never reached the handler. `resolve_pool_size`
   defines the ONE canonical resolution (top-level wins, `provider_config` is
   the pre-DE-11 fallback, else the engine default) used by both sides.
"""

from __future__ import annotations

from typing import Any

# The engine's fallback pool capacity when an operator declares none. One
# source of truth for both the config resolver and the faker handler.
DEFAULT_POOL_SIZE = 10_000


def unique_capacity_ok(pool_size: int, nonnull_output_rows: int) -> bool:
    """Whether a UNIQUE draw of `nonnull_output_rows` values fits in the pool.

    UNIQUE places one distinct pool value in every NON-NULL output row, drawn
    without replacement, so the pool must hold at least that many values. This
    is the single definition shared by the compile-time feasibility check and
    the runtime sampler (DE-11); the source distinct count does not enter.
    """
    return pool_size >= nonnull_output_rows


def resolve_pool_size(col_entry: dict[str, Any]) -> tuple[int, bool]:
    """Resolve a column's pool capacity from the canonical config location.

    Returns ``(pool_size, declared)``. Canonical order (DE-11):

    1. top-level column ``pool_size`` (what operators set) wins;
    2. ``provider_config['pool_size']`` is the pre-DE-11 fallback (many
       existing configs and runtime tests set it there);
    3. otherwise :data:`DEFAULT_POOL_SIZE`.

    ``declared`` is True when either location supplied a value; the faker
    handler only silently accepts the engine default, and fails a
    declared-but-too-small pool closed.
    """
    top = col_entry.get("pool_size")
    if top is not None:
        return int(top), True
    provider_config = col_entry.get("provider_config")
    if isinstance(provider_config, dict) and provider_config.get("pool_size") is not None:
        return int(provider_config["pool_size"]), True
    return DEFAULT_POOL_SIZE, False

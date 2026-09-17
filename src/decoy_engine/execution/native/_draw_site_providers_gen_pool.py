"""GP2 generation-local Faker pool DrawSiteProvider siblings.

Split out of ``_draw_site_providers.py`` for the same reason
``_draw_sites_gen_pool.py`` splits out of ``_determinism_protocol.py``: that
module is allowlisted at its recorded LOC ceiling (``tests/sentry/test_module_size.py``),
so a new provider lands here and is spliced into ``_DEDICATED_PROVIDER_CLASSES``
at its registry-build site, instead of regrowing an already-capped file.

Two providers, matching the two ``gen.faker_pool_build`` / ``gen.faker_pool_selection``
sites in ``_draw_sites_gen_pool.py`` -- see ``generation/_faker_pool.py`` for the
mechanism these reproduce.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from decoy_engine.execution.native._draw_site_providers import (
    DrawSiteProvider,
    _SeededFromBytesNumpyProvider,
)
from decoy_engine.generators.derivation import GenDeriveContext


class FakerPoolBuildLocalProvider(DrawSiteProvider):
    """``gen.faker_pool_build``: GP2's generation-local pool BUILD.

    ``build_seed = gen_ctx.family_bytes("faker_pool_build")[:8]``, then ONE
    ``Faker.seed_instance(int.from_bytes(build_seed, "big"))`` on a fresh
    instance seeds a batch of ``pool_size`` provider calls. NOT
    ``FakerPoolBuildProvider`` (``gen.pool_build_faker``): that site keys off
    ``derive(job_seed, "pool/{provider}/{locale}/{namespace}", config_hash)``,
    the V2 PoolBuilder identity shape; this one keys off the column's OWN
    ``GenDeriveContext`` root, so it is rename-invariant per R3.10 and
    independent of every other column's pool the same way gen.faker_per_row's
    row_int is. Pool identity is a pure function of that root, so partitionable.
    """

    draw_site_id = "gen.faker_pool_build"

    def build_seed(self, gen_ctx: GenDeriveContext) -> bytes:
        return gen_ctx.family_bytes("faker_pool_build")[:8]

    def build_seed_int(self, gen_ctx: GenDeriveContext) -> int:
        return int.from_bytes(self.build_seed(gen_ctx), "big", signed=False)

    def run(
        self,
        faker_inst: Any,
        provider_callable: Callable[..., Any],
        gen_ctx: GenDeriveContext,
        pool_size: int,
        *,
        kwargs: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Reproduce the shipped pool build (``generation/_faker_pool.py:132``)."""
        call_kwargs = kwargs or {}
        faker_inst.seed_instance(self.build_seed_int(gen_ctx))
        return [provider_callable(**call_kwargs) for _ in range(pool_size)]

    def partitioned_draw(self, gen_ctx: GenDeriveContext) -> bytes:
        self.assert_partitionable()
        return self.build_seed(gen_ctx)


class FakerPoolSelectionProvider(_SeededFromBytesNumpyProvider):
    """``gen.faker_pool_selection``: GP2's generation-local REUSE selection.

    ``selection_seed = gen_ctx.family_bytes("faker_pool_selection")[:8]`` seeds
    ``default_rng`` for ``integers(0, pool.size, size=n)`` -- mechanically the
    SAME whole-column draw ``gen.pool_nondeterministic`` makes, but keyed from
    THIS column's own label domain (never job_seed directly), so two pooled
    columns with different config never collapse onto one selection stream.
    Seeded but stream-positional, so NON-partitionable.
    """

    draw_site_id = "gen.faker_pool_selection"

    def selection_seed(self, gen_ctx: GenDeriveContext) -> bytes:
        return gen_ctx.family_bytes("faker_pool_selection")[:8]

    def generator_for_column(self, gen_ctx: GenDeriveContext) -> np.random.Generator:
        return self.generator(self.selection_seed(gen_ctx))


GEN_POOL_PROVIDER_CLASSES: tuple[type[DrawSiteProvider], ...] = (
    FakerPoolBuildLocalProvider,
    FakerPoolSelectionProvider,
)

__all__ = [
    "GEN_POOL_PROVIDER_CLASSES",
    "FakerPoolBuildLocalProvider",
    "FakerPoolSelectionProvider",
]

"""Determinism providers for the GEN-UNIQUE draw sites (`generation/_unique.py`).

Whole-column python Random sampling without replacement (CPython `random.Random.sample`) for
categorical, reference and pair draws, and the per-row retry reseed of a unique faker row.
Re-exported through `_draw_site_providers.py`.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any

from decoy_engine.execution.native.protocol.providers_base import DrawSiteProvider
from decoy_engine.generators.derivation import GenDeriveContext

# Faker's `_UNIQUE_ATTEMPTS`; mirrors `generation/_unique.MAX_ATTEMPTS`.
_MAX_ATTEMPTS = 1000


class _SampleProvider(DrawSiteProvider):
    """One `random.Random(...).sample` over the whole column: NON-partitionable."""

    def rng(self, gen_ctx: GenDeriveContext, family: str = "py") -> random.Random:
        return random.Random(gen_ctx.base_int(family))


class CategoricalUniqueProvider(_SampleProvider):
    """``gen.categorical_unique``: ``sample(distinct, min(n, len))``, ``None`` fills the rest."""

    draw_site_id = "gen.categorical_unique"

    def sample(self, gen_ctx: GenDeriveContext, distinct: Sequence[Any], n: int) -> list[Any]:
        take = min(n, len(distinct))
        out: list[Any] = self.rng(gen_ctx).sample(list(distinct), take)
        out.extend([None] * (n - take))
        return out


class ReferenceUniqueProvider(_SampleProvider):
    """``gen.reference_unique``: ``sample(ref_vals, n)``; ``sequential`` slices ``ref_vals[:n]``."""

    draw_site_id = "gen.reference_unique"

    def sample(
        self,
        gen_ctx: GenDeriveContext,
        ref_vals: Sequence[Any],
        n: int,
        *,
        distribution: str = "random",
    ) -> list[Any]:
        if distribution == "sequential":
            return list(ref_vals[:n])
        return self.rng(gen_ctx).sample(list(ref_vals), n)


class UniquePairProvider(_SampleProvider):
    """``gen.unique_pair``: ``sample(range(P_a * P_b), n)`` decoded to ``(a, b)`` key indices."""

    draw_site_id = "gen.unique_pair"

    def picks(self, gen_ctx: GenDeriveContext, size: int, n: int) -> list[int]:
        return self.rng(gen_ctx, "unique_pair").sample(range(size), n)


class FakerUniqueRetryProvider(DrawSiteProvider):
    """``gen.faker_unique_retry``: retry ``k`` of row ``i`` reseeds from its own HMAC.

    The seen set makes a row depend on earlier rows' values, so the site is NON-partitionable.
    """

    draw_site_id = "gen.faker_unique_retry"

    def retry_seed(self, gen_ctx: GenDeriveContext, i: int, attempts: int) -> int:
        return gen_ctx.row_int("faker_unique", i * _MAX_ATTEMPTS + attempts - 1)


GEN_UNIQUE_PROVIDER_CLASSES: tuple[type[DrawSiteProvider], ...] = (
    FakerUniqueRetryProvider,
    CategoricalUniqueProvider,
    ReferenceUniqueProvider,
    UniquePairProvider,
)

__all__ = [
    "GEN_UNIQUE_PROVIDER_CLASSES",
    "CategoricalUniqueProvider",
    "FakerUniqueRetryProvider",
    "ReferenceUniqueProvider",
    "UniquePairProvider",
]

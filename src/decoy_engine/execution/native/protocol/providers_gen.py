"""Generation-side per-draw-site determinism providers (native program, Task 0.3).

The whole-column numpy and python Random streams, the per-row reseeds
(statistical, formula, Faker), the Faker value-pool build and the
``GenDeriveContext`` substrate. Re-exported from ``_draw_site_providers.py``;
see its docstring for the cited methodology.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from decoy_engine.determinism import derive
from decoy_engine.execution.native.protocol.providers_base import DrawSiteProvider
from decoy_engine.generators.derivation import GenDeriveContext

# ---------------------------------------------------------------------------
# numpy_pcg64 whole-column streams: NON-partitionable (stream-positional).
# ---------------------------------------------------------------------------


class _SeededNumpyBaseIntProvider(DrawSiteProvider):
    """Whole-column numpy stream seeded from ``GenDeriveContext.base_int("np")``.

    Covers ``gen.null_probability`` and ``gen.distribution_snapshot``: one
    contiguous ``default_rng(col_seed)`` stream over the whole column. NON-partitionable.
    """

    def col_generator(self, gen_ctx: GenDeriveContext) -> np.random.Generator:
        return np.random.default_rng(gen_ctx.base_int("np"))


class NullProbabilityProvider(_SeededNumpyBaseIntProvider):
    """``gen.null_probability``: ``default_rng(base_int("np")).random(n) < null_prob``."""

    draw_site_id = "gen.null_probability"

    def null_floats(self, gen_ctx: GenDeriveContext, n: int) -> np.ndarray:
        return self.col_generator(gen_ctx).random(n)


class DistributionSnapshotProvider(_SeededNumpyBaseIntProvider):
    """``gen.distribution_snapshot``: whole-column ``choice`` / ``uniform`` / ``random``."""

    draw_site_id = "gen.distribution_snapshot"


class _SeededFromBytesNumpyProvider(DrawSiteProvider):
    """Whole-column numpy stream seeded from ``int.from_bytes(seed, "big")``.

    Covers ``gen.pool_nondeterministic`` (seed = pool build seed) and
    ``gen.composite_build_pool`` (seed = ``spec.seed`` or ``b"\\x00" * 8``).
    Seeded but stream-positional, so NON-partitionable.
    """

    def generator(self, seed: bytes) -> np.random.Generator:
        return np.random.default_rng(int.from_bytes(seed, "big"))


class PoolNonDeterministicProvider(_SeededFromBytesNumpyProvider):
    """``gen.pool_nondeterministic``: ``integers(0, pool.size, size=n)`` / ``permutation(...)[:k]``."""

    draw_site_id = "gen.pool_nondeterministic"


class CompositeBuildPoolProvider(_SeededFromBytesNumpyProvider):
    """``gen.composite_build_pool``: build-time ``integers`` / ``bytes(8)`` pool fill."""

    draw_site_id = "gen.composite_build_pool"

    def generator(self, seed: bytes | None) -> np.random.Generator:
        return super().generator(seed if seed else b"\x00" * 8)


# ---------------------------------------------------------------------------
# python_mt19937 whole-column streams: NON-partitionable.
# ---------------------------------------------------------------------------


class CategoricalChoicesProvider(DrawSiteProvider):
    """``gen.categorical``: ``random.Random(base_int("py")).choices(cats, weights, k=n)``."""

    draw_site_id = "gen.categorical"

    def rng(self, gen_ctx: GenDeriveContext) -> random.Random:
        return random.Random(gen_ctx.base_int("py"))

    def choices(
        self,
        gen_ctx: GenDeriveContext,
        categories: Sequence[Any],
        weights: Sequence[float] | None,
        n: int,
    ) -> list[Any]:
        return self.rng(gen_ctx).choices(list(categories), weights=weights, k=n)


class ReferenceSamplerProvider(DrawSiteProvider):
    """``gen.reference``: one ``random.Random(base_int("py"))`` advanced across all rows.

    ``random`` -> per-row ``choice``; ``weighted`` -> per-row ``choices(..., k=1)``;
    ``sequential`` -> ``ref_vals[i % len]`` (no draw). One stream, so NON-partitionable.
    """

    draw_site_id = "gen.reference"

    def rng(self, gen_ctx: GenDeriveContext) -> random.Random:
        return random.Random(gen_ctx.base_int("py"))

    def sample(
        self,
        gen_ctx: GenDeriveContext,
        ref_vals: Sequence[Any],
        n: int,
        *,
        distribution: str = "random",
        weights: Sequence[float] | None = None,
    ) -> list[Any]:
        rng = self.rng(gen_ctx)
        vals = list(ref_vals)
        out: list[Any] = []
        for i in range(n):
            if distribution == "sequential":
                out.append(vals[i % len(vals)])
            elif distribution == "weighted":
                w = weights if (weights and len(weights) == len(vals)) else None
                out.append(rng.choices(vals, weights=w, k=1)[0])
            else:
                out.append(rng.choice(vals))
        return out


# ---------------------------------------------------------------------------
# per_row_reseed: one Random reseeded every row from a per-row key. Partitionable.
# ---------------------------------------------------------------------------


class StatisticalPerRowProvider(DrawSiteProvider):
    """``gen.statistical_per_row``: ``rng.seed(col_seed + i)`` per row (legacy idiom).

    One ``random.Random`` reseeded each row from ``col_seed + i``, then an
    inverse-CDF / weighted draw. Reseed-per-row makes any chunking byte-identical
    to a serial pass, so partitionable. ``freetext`` draws a variable number of
    calls within the row, but the per-row reseed contains it.
    """

    draw_site_id = "gen.statistical_per_row"

    def row_seed(self, col_seed: int, i: int) -> int:
        return col_seed + i

    def reseed_row(self, rng: random.Random, col_seed: int, i: int) -> random.Random:
        rng.seed(self.row_seed(col_seed, i))
        return rng

    def partitioned_draw(self, col_seed: int, i: int) -> int:
        self.assert_partitionable()
        return self.row_seed(col_seed, i)


class FormulaPerRowProvider(DrawSiteProvider):
    """``gen.formula_per_row``: per-row reseed of BOTH a ``random.Random`` and Faker.

    Per row ``i``: ``row_rng.seed(gen_ctx.row_int("py", i))`` and
    ``faker.seed_instance(gen_ctx.row_int("faker", i))``, then the formula body.
    Cross-row order does not matter, so partitionable.
    """

    draw_site_id = "gen.formula_per_row"

    def py_row_seed(self, gen_ctx: GenDeriveContext, i: int) -> int:
        return gen_ctx.row_int("py", i)

    def faker_row_seed(self, gen_ctx: GenDeriveContext, i: int) -> int:
        return gen_ctx.row_int("faker", i)

    def reseed_row(
        self, row_rng: random.Random, faker_inst: Any, gen_ctx: GenDeriveContext, i: int
    ) -> None:
        row_rng.seed(self.py_row_seed(gen_ctx, i))
        faker_inst.seed_instance(self.faker_row_seed(gen_ctx, i))

    def partitioned_draw(self, gen_ctx: GenDeriveContext, i: int) -> tuple[int, int]:
        self.assert_partitionable()
        return (self.py_row_seed(gen_ctx, i), self.faker_row_seed(gen_ctx, i))


# ---------------------------------------------------------------------------
# faker_seed_instance: reseed a detached Faker random.Random per row / span.
# ---------------------------------------------------------------------------


class FakerPerRowProvider(DrawSiteProvider):
    """``gen.faker_per_row``: ``seed_instance(gen_ctx.row_int("faker", i))`` per row.

    Each row reseeds the Faker instance from its own HMAC(i), then draws
    ``provider_func(**kwargs)``. Draw count varies by provider, but the per-row
    reseed keys on ``i`` alone, so partitionable.
    """

    draw_site_id = "gen.faker_per_row"

    def row_seed(self, gen_ctx: GenDeriveContext, i: int) -> int:
        return gen_ctx.row_int("faker", i)

    def run(
        self,
        faker_inst: Any,
        provider_func: Callable[..., Any],
        gen_ctx: GenDeriveContext,
        n: int,
        *,
        kwargs: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Reproduce the shipped per-row loop (``synthesize.py:490``)."""
        call_kwargs = kwargs or {}
        out: list[Any] = []
        for i in range(n):
            faker_inst.seed_instance(self.row_seed(gen_ctx, i))
            out.append(provider_func(**call_kwargs))
        return out

    def partitioned_draw(self, gen_ctx: GenDeriveContext, i: int) -> int:
        self.assert_partitionable()
        return self.row_seed(gen_ctx, i)


class FakerPoolBuildProvider(DrawSiteProvider):
    """``gen.pool_build_faker``: build a bounded value pool once under a derived seed.

    ``pool_seed = derive(job_seed, "pool/{provider}/{locale}/{namespace}", config_hash)[:8]``
    (``_derive_pool_seed``), threaded into the adapter as ``ProviderSpec.seed``; the
    Faker adapter then ``seed_instance(int.from_bytes(pool_seed, "big"))``. Pool
    identity is a pure function of ``(job_seed, provider, locale, config, namespace)``,
    so a cached and a rebuilt pool of the same identity are value-identical.
    """

    draw_site_id = "gen.pool_build_faker"

    def pool_seed(
        self,
        job_seed: bytes,
        provider: str,
        locale: str | None,
        namespace: str | None,
        config_hash: str,
    ) -> bytes:
        pool_namespace = f"pool/{provider}/{locale or 'default'}/{namespace or '_default'}"
        return derive(job_seed, pool_namespace, config_hash.encode("utf-8"))[:8]

    def faker_seed_int(
        self,
        job_seed: bytes,
        provider: str,
        locale: str | None,
        namespace: str | None,
        config_hash: str,
    ) -> int:
        return int.from_bytes(
            self.pool_seed(job_seed, provider, locale, namespace, config_hash), "big"
        )

    def partitioned_draw(
        self,
        job_seed: bytes,
        provider: str,
        locale: str | None,
        namespace: str | None,
        config_hash: str,
    ) -> bytes:
        self.assert_partitionable()
        return self.pool_seed(job_seed, provider, locale, namespace, config_hash)


# ---------------------------------------------------------------------------
# gen_derive_context substrate: the keying layer, not a draw itself.
# ---------------------------------------------------------------------------


class GenDeriveContextProvider(DrawSiteProvider):
    """``gen.derive_context``: the ``GenDeriveContext`` seed-derivation substrate.

    ``base_int(family)`` seeds whole-column consumers (which may not be
    partitionable); ``row_int(family, i)`` is a pure function of ``(root, family,
    i)`` and is partitionable. This provider re-exposes both without changing them.
    """

    draw_site_id = "gen.derive_context"

    def base_int(self, gen_ctx: GenDeriveContext, family: str) -> int:
        return gen_ctx.base_int(family)

    def row_int(self, gen_ctx: GenDeriveContext, family: str, i: int) -> int:
        return gen_ctx.row_int(family, i)

    def partitioned_draw(self, gen_ctx: GenDeriveContext, family: str, i: int) -> int:
        self.assert_partitionable()
        return gen_ctx.row_int(family, i)

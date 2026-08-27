"""Per-draw-site determinism providers (native program, Task 0.3).

One :class:`DrawSiteProvider` per catalogued :class:`DrawSite`
(``_determinism_protocol.DRAW_SITES``). Each provider is a PURE function of
its site's declared identity tuple ``(entropy_root, strategy/config
fingerprint, table/group/row identity, draw ordinal, variable-draw
algorithm, provider version)`` and reproduces EXACTLY the sequence the
current engine draws at that site: the same seed derivation, the same RNG
object construction, the same API operation and call shape, and the same
null-consumption rule.

Why per-draw-site and not one generator, nor per-family: the SAME RNG family
appears at DIFFERENT sites with DIFFERENT seed derivations and DIFFERENT call
shapes. ``mask.shuffle`` and ``mask.grouped_series_monotone_walk`` are both
``numpy_pcg64`` yet one draws a whole-column ``permutation(n)`` seeded from
``derive(mask_key, namespace, column)`` and the other advances a per-group
stream seeded per group label. A single HMAC-counter generator cannot
reproduce either sequence, and per-family versioning collapses the two. So
the protocol versions semantics by ``draw_site_id`` within each family; each
provider carries its site's ``provider_version`` verbatim.

Partitionability is a property of the draw, not a wish. A whole-column stream
(``permutation(n)``, ``choices(k=n)``, ``default_rng.random(n)``) and a
per-group sequential walk cannot be reproduced by concatenated local draws or
a per-row substream, because a row's value depends on the position of every
earlier draw in the stream. Those providers are ``partitionable=False`` and
REFUSE a partitioned request with a coded error rather than fake a substream.
Per-row source-keyed derivations and per-row-reseeded streams ARE
partitionable: a batch reproduces a row's output from that row's own key.

References (established methodology, per the repo rule):
- NumPy NEP-19 ``numpy.random.default_rng`` / PCG64: the seed-stability
  contract every ``numpy_pcg64`` site relies on
  (https://numpy.org/neps/nep-0019-rng-policy.html).
- CPython ``random.Random`` (Mersenne Twister MT19937): the seed-stability
  contract every ``python_mt19937`` / ``per_row_reseed`` site relies on
  (https://docs.python.org/3/library/random.html).
- RFC 5869 (HKDF-SHA256) + RFC 2104 (HMAC-SHA256) via
  ``decoy_engine.determinism`` (``derive`` / ``derive_index`` /
  ``derive_value`` and ``GenDeriveContext``): the keyed-derivation envelope
  the ``source_keyed_hmac`` / ``faker_seed_instance`` / ``gen_derive_context``
  sites seed from. ``SEED_PROTOCOL_VERSION`` is the single compatibility knob.

This module changes no masking or generation behavior. It reproduces the
existing draws so the native executor can seed identically off the hot path.
"""

from __future__ import annotations

import hashlib
import hmac
import random
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from decoy_engine.determinism import derive, derive_index, derive_value
from decoy_engine.determinism._derive import Domain
from decoy_engine.execution.native._determinism_protocol import (
    DRAW_SITES,
    DrawSite,
    draw_site_by_id,
)
from decoy_engine.generators.derivation import GenDeriveContext

# 2**53. The full mantissa width of an IEEE-754 double. NumPy's own
# Generator.random() builds a float64 in [0, 1) as (upper 53 bits) / 2**53.
_TWO_POW_53 = 9007199254740992  # 1 << 53
_U64_HI = 1 << 64


def unit_float_from_bits53(raw_u64: int) -> float:
    """Return a float in ``[0, 1)`` from the UPPER 53 bits of a full u64.

    This is NumPy's own ``random()`` construction: take a 64-bit draw, discard
    the low 11 bits, and divide the remaining 53-bit integer by ``2**53``. The
    input is a FULL 64-bit value; the shift extracts the upper 53 bits, so the
    result is always ``< 1.0`` even for the all-ones input:
    ``(((1 << 64) - 1) >> 11) / 2**53 == (2**53 - 1) / 2**53 < 1.0``.

    Never ``raw_u64 / 2**64`` (that can round up to 1.0), and never assume the
    input is already a 53-bit value.

    Raises:
        ValueError: if ``raw_u64`` is outside ``[0, 2**64)``.
    """
    if not 0 <= raw_u64 < _U64_HI:
        raise ValueError(f"raw_u64 must be a full unsigned 64-bit value; got {raw_u64}")
    return (raw_u64 >> 11) / _TWO_POW_53


class DrawSiteProtocolError(RuntimeError):
    """Coded failure raised by a draw-site provider.

    Mirrors the repo's kwargs-only coded-error shape (``DeterminismError``,
    ``StrategyError``) so callers can ``except DrawSiteProtocolError as e:
    e.code`` consistently.

    Codes:
        site_not_partitionable: a partitioned draw was requested from a
            provider whose site is ``partitionable=False`` (a whole-column or
            per-group stream). Phase 1 cannot route these; they stay on the
            full-frame oracle until Phase 4.
        site_not_reproducible: a reproduction was requested from a provider
            whose site is unseeded by contract (non-deterministic mode). Its
            output differs run to run by design; there is no sequence to
            reproduce.
    """

    def __init__(self, *, code: str, draw_site_id: str, message: str = "") -> None:
        self.code = code
        self.draw_site_id = draw_site_id
        self.message = message
        detail = f"{code} [{draw_site_id}]"
        super().__init__(f"{detail}: {message}" if message else detail)


class DrawSiteProvider:
    """Base class: a per-site pure reproduction of one catalogued draw.

    Subclasses reproduce the exact seed derivation and draw mechanism of one
    ``draw_site_id``. The base exposes the site's frozen metadata and the
    partition contract every subclass shares.
    """

    #: The single ``draw_site_id`` this provider class reproduces. Subclasses
    #: that reproduce several catalogued ids (the ``source_keyed_hmac`` family
    #: shares one mechanism) leave this None and are registered explicitly.
    draw_site_id: str | None = None

    def __init__(self, site: DrawSite) -> None:
        self.site = site

    @property
    def family(self) -> str:
        return self.site.family

    @property
    def entropy_root(self) -> str:
        return self.site.entropy_root

    @property
    def partitionable(self) -> bool:
        return self.site.partitionable

    @property
    def provider_version(self) -> str:
        return self.site.provider_version

    @property
    def consumes_variable_draws(self) -> bool:
        return self.site.consumes_variable_draws

    def assert_partitionable(self) -> None:
        """Raise ``site_not_partitionable`` when the site cannot be partitioned.

        Every non-partitionable provider routes its partitioned entry points
        through this so a partition request fails loudly instead of silently
        returning a wrong (locally-fabricated) sequence.
        """
        if not self.site.partitionable:
            raise DrawSiteProtocolError(
                code="site_not_partitionable",
                draw_site_id=self.site.draw_site_id,
                message=(
                    "whole-column or per-group stream: a partition boundary breaks "
                    "the draw order, so a partitioned pass cannot reproduce it. "
                    "Stays on the full-frame oracle until Phase 4."
                ),
            )

    def partitioned_draw(self, *args: Any, **kwargs: Any) -> Any:
        """Reproduce the draw for one partition/batch.

        Non-partitionable providers refuse here (coded error). Partitionable
        providers override with a real per-partition reproduction.
        """
        self.assert_partitionable()
        raise NotImplementedError(  # pragma: no cover - partitionable subclasses override
            f"{type(self).__name__}.partitioned_draw not implemented"
        )


# ---------------------------------------------------------------------------
# source_keyed_hmac family: the digest IS the randomness (no RNG object).
# Per-row, source-keyed, partitionable. One provider mechanism, one instance
# per catalogued id so versioning stays per-site.
# ---------------------------------------------------------------------------


class SourceKeyedHmacProvider(DrawSiteProvider):
    """A per-row source-keyed derivation over ``decoy_engine.determinism``.

    ``primitive`` selects which keyed primitive the site draws through:
    ``derive`` (32-byte digest, used raw or hex), ``derive_index`` (digest
    reduced to ``[0, pool_size)``), or ``derive_value`` (domain-typed wrapper).
    The provider is a pure function of ``(seed, namespace, source)`` (plus
    ``pool_size`` / ``domain``), exactly as the shipped call is, so a partition
    reproduces a row from that row's own key.
    """

    def __init__(self, site: DrawSite, *, primitive: str) -> None:
        super().__init__(site)
        if primitive not in ("derive", "derive_index", "derive_value"):
            raise ValueError(f"unknown keyed primitive {primitive!r}")
        self.primitive = primitive

    def draw(
        self,
        seed: bytes,
        namespace: str,
        source: bytes,
        *,
        pool_size: int | None = None,
        domain: Domain | None = None,
    ) -> Any:
        """Reproduce the site's keyed draw for one source value.

        ``seed`` is the site's entropy root: ``mask_key`` for masking sites,
        ``job_seed`` for generation sites. ``source`` is the already-canonical
        source bytes the shipped call keys on (canonicalization is the caller's
        job, the same boundary as ``derive``).
        """
        if self.primitive == "derive":
            return derive(seed, namespace, source)
        if self.primitive == "derive_index":
            if pool_size is None:
                raise ValueError("derive_index site requires pool_size")
            return derive_index(seed, namespace, source, pool_size=pool_size)
        if domain is None:
            raise ValueError("derive_value site requires domain")
        return derive_value(seed, namespace, source, domain=domain)

    def partitioned_draw(
        self,
        seed: bytes,
        namespace: str,
        source: bytes,
        *,
        pool_size: int | None = None,
        domain: Domain | None = None,
    ) -> Any:
        self.assert_partitionable()
        return self.draw(seed, namespace, source, pool_size=pool_size, domain=domain)


def _span_key(mask_key: bytes, matched_text: str) -> bytes:
    """HMAC-SHA256(mask_key, matched_text) -- the text-mask per-span key.

    Verbatim shape of ``transforms/text_mask.py:_span_key``: the key depends on
    the matched text only, so the same real value keys the same replacement.
    """
    return hmac.new(mask_key, matched_text.encode("utf-8"), hashlib.sha256).digest()


class TextMaskDateShiftProvider(DrawSiteProvider):
    """``mask.text_mask_date_shift``: a detected date span shifts by a keyed offset.

    ``span_key = HMAC(mask_key, matched_text)``; the shift is
    ``min_days + (int.from_bytes(span_key[:8], "big") % range_size)``. Keyed on
    the span text, so partitionable.
    """

    draw_site_id = "mask.text_mask_date_shift"

    def shift_days(self, mask_key: bytes, matched_text: str, min_days: int, max_days: int) -> int:
        range_size = max_days - min_days + 1
        offset_seed = int.from_bytes(_span_key(mask_key, matched_text)[:8], "big")
        return min_days + (offset_seed % range_size)

    def partitioned_draw(
        self, mask_key: bytes, matched_text: str, min_days: int, max_days: int
    ) -> int:
        self.assert_partitionable()
        return self.shift_days(mask_key, matched_text, min_days, max_days)


# ---------------------------------------------------------------------------
# numpy_pcg64 whole-column streams: NON-partitionable (stream-positional).
# ---------------------------------------------------------------------------


class ShuffleProvider(DrawSiteProvider):
    """``mask.shuffle``: whole-column multiset permutation (numpy PCG64).

    Seed EXACTLY ``int.from_bytes(derive(mask_key, namespace, column.encode())[:8], "big")``,
    then ``np.random.default_rng(seed).permutation(len(non_null))``. Whole-column,
    so NON-partitionable: a partition sees only its slice and cannot reproduce
    the global order.
    """

    draw_site_id = "mask.shuffle"

    def seed_int(self, mask_key: bytes, namespace: str, column: str) -> int:
        return int.from_bytes(derive(mask_key, namespace, column.encode("utf-8"))[:8], "big")

    def permutation(self, mask_key: bytes, namespace: str, column: str, n: int) -> np.ndarray:
        rng = np.random.default_rng(self.seed_int(mask_key, namespace, column))
        return rng.permutation(n)


class GroupedSeriesWalkProvider(DrawSiteProvider):
    """``mask.grouped_series_monotone_walk``: per-group sequential stream.

    Per group label ``g``: seed
    ``int.from_bytes(derive(mask_key, namespace, str(g).encode("utf-8", "replace"))[:8], "big")``
    seeds ``default_rng(g_seed)``, advanced per row within the group by
    ``integers(step, max_step + 1)`` and accumulated from ``start``. NON-partitionable:
    splitting a group across partitions loses the running sum.
    """

    draw_site_id = "mask.grouped_series_monotone_walk"

    def group_seed_int(self, mask_key: bytes, namespace: str, group_label: Any) -> int:
        source = str(group_label).encode("utf-8", errors="replace")
        return int.from_bytes(derive(mask_key, namespace, source)[:8], "big")

    def group_generator(
        self, mask_key: bytes, namespace: str, group_label: Any
    ) -> np.random.Generator:
        return np.random.default_rng(self.group_seed_int(mask_key, namespace, group_label))

    def walk(
        self,
        mask_key: bytes,
        namespace: str,
        group_label: Any,
        group_len: int,
        *,
        start: int,
        step: int,
        max_step: int,
    ) -> list[int]:
        """Reproduce one group's monotone walk: ``start`` then cumulative steps."""
        rng = self.group_generator(mask_key, namespace, group_label)
        out: list[int] = []
        cumulative = start
        for _ in range(group_len):
            out.append(cumulative)
            cumulative += int(rng.integers(step, max_step + 1))
        return out


class WindowedDateProvider(DrawSiteProvider):
    """``mask.windowed_date``: fresh per-row numpy Generator seeded by row index.

    Per row ``i``: seed
    ``int.from_bytes(derive(mask_key, namespace, i.to_bytes(8, "big"))[:8], "big")``,
    then ``default_rng(row_seed).integers(min_days, max_days + 1)`` (1 draw for
    ``uniform``, 2 for ``early``/``late``). Keys on the GLOBAL row index, so
    partitionable only when the native executor pins ``i`` to a global row number.
    """

    draw_site_id = "mask.windowed_date"

    def row_seed_int(self, mask_key: bytes, namespace: str, global_row_index: int) -> int:
        source = global_row_index.to_bytes(8, "big")
        return int.from_bytes(derive(mask_key, namespace, source)[:8], "big")

    def row_generator(
        self, mask_key: bytes, namespace: str, global_row_index: int
    ) -> np.random.Generator:
        return np.random.default_rng(self.row_seed_int(mask_key, namespace, global_row_index))

    def partitioned_draw(
        self, mask_key: bytes, namespace: str, global_row_index: int
    ) -> np.random.Generator:
        """A partition reproduces row ``i`` from its GLOBAL index, unchanged."""
        self.assert_partitionable()
        return self.row_generator(mask_key, namespace, global_row_index)


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


class MaskFormulaProvider(DrawSiteProvider):
    """``mask.formula``: one ``random.Random`` self-seeded from the formula text.

    ``formula_seed = int(sha256(f"{column}|{formula}").hexdigest()[:16], 16)``,
    then ``random.Random(formula_seed)`` shared across all rows via
    ``column.apply``. Order-dependent (a row draws only if the formula calls the
    RNG), so NON-partitionable, and independent of ``mask_key`` by design.
    """

    draw_site_id = "mask.formula"

    def formula_seed(self, column: str, formula: str) -> int:
        seed_material = f"{column}|{formula}".encode()
        return int(hashlib.sha256(seed_material).hexdigest()[:16], 16)

    def rng(self, column: str, formula: str) -> random.Random:
        return random.Random(self.formula_seed(column, formula))


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


class TextMaskFakerProvider(DrawSiteProvider):
    """``mask.text_mask_faker``: reseed Faker from a source-keyed span key.

    ``seed = int.from_bytes(span_key[:4], "big")`` where
    ``span_key = HMAC(mask_key, matched_text)``, then ``Faker().seed_instance(seed)``
    and a provider method per detected span. Keyed on the span text, so partitionable.
    """

    draw_site_id = "mask.text_mask_faker"

    def span_seed(self, mask_key: bytes, matched_text: str) -> int:
        return int.from_bytes(_span_key(mask_key, matched_text)[:4], "big")

    def run(self, faker_inst: Any, method_name: str, mask_key: bytes, matched_text: str) -> Any:
        faker_inst.seed_instance(self.span_seed(mask_key, matched_text))
        return getattr(faker_inst, method_name)()

    def partitioned_draw(self, mask_key: bytes, matched_text: str) -> int:
        self.assert_partitionable()
        return self.span_seed(mask_key, matched_text)


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


# ---------------------------------------------------------------------------
# Non-deterministic-by-contract sites: unseeded default_rng(). No golden;
# refuse partition (and reproduction) honestly rather than fake a stream.
# ---------------------------------------------------------------------------


class UnseededProvider(DrawSiteProvider):
    """An unseeded ``np.random.default_rng()`` site (non-deterministic by contract).

    Covers ``mask.categorical_nondeterministic`` and
    ``gen.identifier_nondeterministic``: output differs run to run by design, so
    there is nothing to reproduce and no partition to serve. ``fresh_generator``
    hands back a genuinely unseeded Generator; ``reproduce``/``partitioned_draw``
    refuse with a coded error so no caller mistakes this for a stable stream.
    """

    def __init__(self, site: DrawSite) -> None:
        super().__init__(site)

    def fresh_generator(self) -> np.random.Generator:
        return np.random.default_rng()

    def reproduce(self, *args: Any, **kwargs: Any) -> Any:
        raise DrawSiteProtocolError(
            code="site_not_reproducible",
            draw_site_id=self.site.draw_site_id,
            message="unseeded non-deterministic site: output differs run to run by design.",
        )

    def partitioned_draw(self, *args: Any, **kwargs: Any) -> Any:
        # Non-deterministic AND non-partitionable: the partition contract error
        # is the right one for a routing caller.
        self.assert_partitionable()
        raise NotImplementedError  # pragma: no cover - never partitionable


# ---------------------------------------------------------------------------
# The registry: exactly one provider instance per catalogued draw_site_id.
# ---------------------------------------------------------------------------

# Which keyed primitive each source_keyed_hmac site draws through.
_SOURCE_KEYED_PRIMITIVE: dict[str, str] = {
    "mask.hash": "derive",
    "mask.fpe": "derive",
    "mask.date_shift": "derive",
    "mask.bucket_perturb": "derive",
    "mask.group_key": "derive",
    "mask.joint_mask_keyed_row": "derive",
    "mask.categorical_deterministic": "derive_index",
    "mask.code_set": "derive_index",
    "gen.pool_deterministic": "derive_index",
    "mask.faker": "derive_index",
    "gen.identifier_deterministic": "derive_value",
}

# Dedicated mechanism providers, keyed by the single id each reproduces.
_DEDICATED_PROVIDER_CLASSES: tuple[type[DrawSiteProvider], ...] = (
    ShuffleProvider,
    GroupedSeriesWalkProvider,
    WindowedDateProvider,
    NullProbabilityProvider,
    DistributionSnapshotProvider,
    PoolNonDeterministicProvider,
    CompositeBuildPoolProvider,
    CategoricalChoicesProvider,
    ReferenceSamplerProvider,
    MaskFormulaProvider,
    StatisticalPerRowProvider,
    FormulaPerRowProvider,
    FakerPerRowProvider,
    TextMaskFakerProvider,
    TextMaskDateShiftProvider,
    FakerPoolBuildProvider,
    GenDeriveContextProvider,
)

# Unseeded, non-deterministic-by-contract sites.
_UNSEEDED_SITE_IDS: frozenset[str] = frozenset(
    {"mask.categorical_nondeterministic", "gen.identifier_nondeterministic"}
)


def _build_registry() -> dict[str, DrawSiteProvider]:
    registry: dict[str, DrawSiteProvider] = {}

    for cls in _DEDICATED_PROVIDER_CLASSES:
        site_id = cls.draw_site_id
        if site_id is None:
            raise RuntimeError(f"dedicated provider {cls.__name__} declares no draw_site_id")
        registry[site_id] = cls(draw_site_by_id(site_id))

    for site_id, primitive in _SOURCE_KEYED_PRIMITIVE.items():
        registry[site_id] = SourceKeyedHmacProvider(draw_site_by_id(site_id), primitive=primitive)

    for site_id in _UNSEEDED_SITE_IDS:
        registry[site_id] = UnseededProvider(draw_site_by_id(site_id))

    return registry


_PROVIDERS: dict[str, DrawSiteProvider] = _build_registry()


def provider_for(draw_site_id: str) -> DrawSiteProvider:
    """Return the single :class:`DrawSiteProvider` for ``draw_site_id``.

    Raises:
        KeyError: if no provider is registered (i.e. an uncatalogued site).
    """
    try:
        return _PROVIDERS[draw_site_id]
    except KeyError as exc:
        raise KeyError(f"no determinism provider for draw_site_id {draw_site_id!r}") from exc


def all_providers() -> dict[str, DrawSiteProvider]:
    """Return a copy of the full ``draw_site_id -> provider`` registry."""
    return dict(_PROVIDERS)


# Fail loudly at import time if the registry drifts from the catalogue: every
# catalogued site must have exactly one provider, and every provider must map
# to a catalogued site. This is the per-site totality invariant Task 0.3 freezes.
_CATALOGUED_IDS = frozenset(s.draw_site_id for s in DRAW_SITES)
_REGISTERED_IDS = frozenset(_PROVIDERS)
if _CATALOGUED_IDS != _REGISTERED_IDS:
    _missing = _CATALOGUED_IDS - _REGISTERED_IDS
    _extra = _REGISTERED_IDS - _CATALOGUED_IDS
    raise RuntimeError(
        "draw-site provider registry drift: "
        f"missing providers for {sorted(_missing)}; "
        f"providers with no catalogue entry {sorted(_extra)}"
    )


__all__ = [
    "CategoricalChoicesProvider",
    "CompositeBuildPoolProvider",
    "DistributionSnapshotProvider",
    "DrawSiteProtocolError",
    "DrawSiteProvider",
    "FakerPerRowProvider",
    "FakerPoolBuildProvider",
    "FormulaPerRowProvider",
    "GenDeriveContextProvider",
    "GroupedSeriesWalkProvider",
    "MaskFormulaProvider",
    "NullProbabilityProvider",
    "PoolNonDeterministicProvider",
    "ReferenceSamplerProvider",
    "ShuffleProvider",
    "SourceKeyedHmacProvider",
    "StatisticalPerRowProvider",
    "TextMaskDateShiftProvider",
    "TextMaskFakerProvider",
    "UnseededProvider",
    "WindowedDateProvider",
    "all_providers",
    "provider_for",
    "unit_float_from_bits53",
]

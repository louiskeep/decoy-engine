"""Shared base of the per-draw-site determinism providers (native program, Task 0.3).

The partition contract every provider shares (``DrawSiteProvider``), its coded
error (``DrawSiteProtocolError``), NumPy's 53-bit unit-float construction, and
the two mechanisms that serve sites on both the masking and the generation side:
the single-primitive source-keyed HMAC derivation and the unseeded,
non-deterministic-by-contract generator. ``_draw_site_providers.py`` re-exports
all of it; see that module's docstring for the per-draw-site rationale and the
cited methodology (NumPy NEP-19, CPython ``random``, RFC 5869 / RFC 2104).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from decoy_engine import reasons
from decoy_engine.determinism import derive, derive_index, derive_value
from decoy_engine.determinism._derive import Domain
from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from decoy_engine.execution.native._determinism_protocol import DrawSite

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


class DrawSiteProtocolError(DecoyError, RuntimeError):
    """Coded failure raised by a draw-site provider.

    Mirrors the repo's kwargs-only coded-error shape (``DeterminismError``, ``StrategyError``)
    so callers can ``except DrawSiteProtocolError as e: e.code`` consistently.

    Codes:
        site_not_partitionable: a partitioned draw was requested from a provider whose site is
            ``partitionable=False`` (a whole-column or per-group stream). Phase 1 cannot route
            these; they stay on the full-frame oracle until Phase 4.
        site_not_reproducible: a reproduction was requested from a provider whose site is
            unseeded by contract (non-deterministic mode). Its output differs run to run by
            design; there is no sequence to reproduce.
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
                code=reasons.runtime.SITE_NOT_PARTITIONABLE,
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


# ---------------------------------------------------------------------------
# Non-deterministic-by-contract sites: unseeded default_rng(). No golden;
# refuse partition (and reproduction) honestly rather than fake a stream.
# ---------------------------------------------------------------------------


class UnseededProvider(DrawSiteProvider):
    """An unseeded ``np.random.default_rng()`` site (non-deterministic by contract).

    Covers ``gen.identifier_nondeterministic``: output differs run to run by
    design, so there is nothing to reproduce and no partition to serve. ``fresh_generator``
    hands back a genuinely unseeded Generator; ``reproduce``/``partitioned_draw``
    refuse with a coded error so no caller mistakes this for a stable stream.
    """

    def __init__(self, site: DrawSite) -> None:
        super().__init__(site)

    def fresh_generator(self) -> np.random.Generator:
        return np.random.default_rng()

    def reproduce(self, *args: Any, **kwargs: Any) -> Any:
        raise DrawSiteProtocolError(
            code=reasons.runtime.SITE_NOT_REPRODUCIBLE,
            draw_site_id=self.site.draw_site_id,
            message="unseeded non-deterministic site: output differs run to run by design.",
        )

    def partitioned_draw(self, *args: Any, **kwargs: Any) -> Any:
        # Non-deterministic AND non-partitionable: the partition contract error
        # is the right one for a routing caller.
        self.assert_partitionable()
        raise NotImplementedError  # pragma: no cover - never partitionable

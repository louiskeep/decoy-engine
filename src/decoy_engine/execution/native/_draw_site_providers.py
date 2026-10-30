"""Per-draw-site determinism providers (native program, Task 0.3).

One :class:`DrawSiteProvider` per catalogued :class:`DrawSite`
(``_determinism_protocol.DRAW_SITES``). Each provider is a PURE function of its site's declared
identity tuple ``(entropy_root, strategy/config fingerprint, table/group/row identity, draw
ordinal, variable-draw algorithm, provider version)`` and reproduces EXACTLY the sequence the
current engine draws at that site: the same seed derivation, the same RNG object construction,
the same API operation and call shape, and the same null-consumption rule.

Why per-draw-site and not one generator, nor per-family: the SAME RNG family appears at
DIFFERENT sites with DIFFERENT seed derivations and DIFFERENT call shapes. ``mask.shuffle`` and
``mask.grouped_series_monotone_walk`` are both ``numpy_pcg64`` yet one draws a whole-column
``permutation(n)`` seeded from ``derive(mask_key, namespace, column)`` and the other advances a
per-group stream seeded per group label. A single HMAC-counter generator cannot reproduce
either sequence, and per-family versioning collapses the two. So the protocol versions
semantics by ``draw_site_id`` within each family; each provider carries its site's
``provider_version`` verbatim.

Partitionability is a property of the draw, not a wish. A whole-column stream
(``permutation(n)``, ``choices(k=n)``, ``default_rng.random(n)``) and a per-group sequential
walk cannot be reproduced by concatenated local draws or a per-row substream, because a row's
value depends on the position of every earlier draw in the stream. Those providers are
``partitionable=False`` and REFUSE a partitioned request with a coded error rather than fake a
substream. Per-row source-keyed derivations and per-row-reseeded streams ARE partitionable: a
batch reproduces a row's output from that row's own key.

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

Layout: the shared base (``DrawSiteProvider``, ``DrawSiteProtocolError``, the
source-keyed HMAC and unseeded mechanisms) lives in ``protocol/providers_base.py``,
the masking-side providers in ``protocol/providers_mask.py`` and the
generation-side providers in ``protocol/providers_gen.py``. This module keeps the
text-mask span providers beside their ``_span_key``, builds the one-provider-per-site
registry, and re-exports the whole provider surface.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from decoy_engine.execution.native._determinism_protocol import (
    DRAW_SITES,
    draw_site_by_id,
)
from decoy_engine.execution.native._draw_site_providers_gen_pool import (
    GEN_POOL_PROVIDER_CLASSES,
)
from decoy_engine.execution.native._draw_sites_tokenize import TOKENIZE_PRIMITIVE
from decoy_engine.execution.native.protocol.providers_base import (
    DrawSiteProtocolError,
    DrawSiteProvider,
    SourceKeyedHmacProvider,
    UnseededProvider,
    unit_float_from_bits53,
)
from decoy_engine.execution.native.protocol.providers_gen import (
    CategoricalChoicesProvider,
    CompositeBuildPoolProvider,
    DistributionSnapshotProvider,
    FakerPerRowProvider,
    FakerPoolBuildProvider,
    FormulaPerRowProvider,
    GenDeriveContextProvider,
    NullProbabilityProvider,
    PoolNonDeterministicProvider,
    ReferenceSamplerProvider,
    StatisticalPerRowProvider,
)

# Private names the determinism drift tests import from here.
from decoy_engine.execution.native.protocol.providers_mask import (
    _CODE_SET_KEYED_SALT,  # noqa: F401
    _FPE_KEY_LABEL,  # noqa: F401
    _JOINT_MASK_KEYED_ROW_SOURCE,  # noqa: F401
    CodeSetKeyedSelectProvider,
    FpeKeyProvider,
    GroupedSeriesWalkProvider,
    JointMaskKeyedRowProvider,
    MaskFormulaProvider,
    ShuffleProvider,
    WindowedDateProvider,
    _hmac_hex,  # noqa: F401
    _hole_resolve,  # noqa: F401
)


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


# ---------------------------------------------------------------------------
# The registry: exactly one provider instance per catalogued draw_site_id.
# ---------------------------------------------------------------------------

# Which keyed primitive each SINGLE-primitive source_keyed_hmac site draws
# through. The compound sites (fpe key, code_set / joint_mask two-step keyed
# selection) have their own dedicated providers in protocol/providers_mask.py, not this map.
_SOURCE_KEYED_PRIMITIVE: dict[str, str] = {
    "mask.hash": "derive",
    "mask.date_shift": "derive",
    "mask.bucket_perturb": "derive",
    "mask.group_key": "derive",
    "mask.categorical_deterministic": "derive_index",
    # Keyed by encode_int(row ordinal) instead of the source value; same primitive.
    "mask.categorical_nondeterministic": "derive_index",
    "gen.pool_deterministic": "derive_index",
    "mask.faker": "derive_index",
    "gen.identifier_deterministic": "derive_value",
} | TOKENIZE_PRIMITIVE

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
    FpeKeyProvider,
    CodeSetKeyedSelectProvider,
    JointMaskKeyedRowProvider,
    *GEN_POOL_PROVIDER_CLASSES,
)

# Unseeded, non-deterministic-by-contract sites.
_UNSEEDED_SITE_IDS: frozenset[str] = frozenset({"gen.identifier_nondeterministic"})


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
    "CodeSetKeyedSelectProvider",
    "CompositeBuildPoolProvider",
    "DistributionSnapshotProvider",
    "DrawSiteProtocolError",
    "DrawSiteProvider",
    "FakerPerRowProvider",
    "FakerPoolBuildProvider",
    "FormulaPerRowProvider",
    "FpeKeyProvider",
    "GenDeriveContextProvider",
    "GroupedSeriesWalkProvider",
    "JointMaskKeyedRowProvider",
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

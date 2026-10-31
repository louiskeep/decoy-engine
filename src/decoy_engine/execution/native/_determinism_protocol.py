"""Machine-checkable RNG draw-site inventory (native program, Task 0.1).

This module is the single source of truth the native columnar-streaming determinism
protocol (Task 0.3) is built against. Every place the engine consumes randomness to
produce masked or generated output is catalogued here as one ``DrawSite`` entry,
recording the EXACT seed derivation and call shape in the code today. Task 0.3 must
reproduce each entry's byte sequence exactly, so accuracy of ``seed_derivation`` and
``call_shape`` is the contract. The prose companion is ``docs/native/draw-site-inventory.md``.

This module is pure data plus small helpers. It imports nothing from the
masking or generation hot paths and changes no behavior.

Partitionability rule (the property the native batched executor cares about): a site is
``partitionable`` when a partitioned or batched pass can reproduce the exact same value for a
given output row as a single serial pass. Per-row source-keyed derivations are partitionable (row
output is a pure function of that row's key). Whole-column ``permutation(n)`` / ``choices(k=n)``
streams and per-group sequential streams are NOT: a row's value depends on the position of every
earlier draw in the stream, which a partition boundary breaks. Per-row-reseeded streams are
partitionable when the reseed key is a stable per-row identity (row index or source value).

Layout: ``DrawSite`` and the allowed enums live in ``_draw_site_types.py``; the
entries live in per-family data modules (``_draw_sites_mask.py``,
``_draw_sites_gen.py``, ``_draw_sites_provider_backed.py``,
``_draw_sites_gen_pool.py``, ``_draw_sites_tokenize.py``). This module splices
them into ``DRAW_SITES`` in catalogue order, owns the coverage maps, and
re-exports the whole protocol surface.
"""

from __future__ import annotations

from decoy_engine.execution.native._draw_site_types import (
    DETERMINISTIC_NO_DRAW,
    ENTROPY_ROOTS,
    FAMILIES,
    IDENTITIES,
    DrawSite,
)
from decoy_engine.execution.native._draw_sites_gen import GEN_DRAW_SITES, GEN_SUBSTRATE_DRAW_SITES
from decoy_engine.execution.native._draw_sites_gen_pool import GEN_POOL_DRAW_SITES
from decoy_engine.execution.native._draw_sites_mask import MASK_DRAW_SITES, MASK_FORMULA_DRAW_SITE
from decoy_engine.execution.native._draw_sites_provider_backed import PROVIDER_BACKED_DRAW_SITES
from decoy_engine.execution.native._draw_sites_tokenize import TOKENIZE_DRAW_SITES

# ---------------------------------------------------------------------------
# The inventory. Grouped by family in the doc; here ordered mask sites first,
# then generation sites, then the derivation substrate, then the sites added
# after the first cut. The order is part of the determinism contract
# (tests/native/test_draw_sites_catalogue_pinned.py pins it): append new
# sites, never reorder.
# ---------------------------------------------------------------------------

DRAW_SITES: tuple[DrawSite, ...] = (
    *MASK_DRAW_SITES,
    *GEN_DRAW_SITES,
    *PROVIDER_BACKED_DRAW_SITES,
    *GEN_SUBSTRATE_DRAW_SITES,
    MASK_FORMULA_DRAW_SITE,
    *GEN_POOL_DRAW_SITES,
    *TOKENIZE_DRAW_SITES,
)

# ---------------------------------------------------------------------------
# Coverage maps. The test cross-checks these against the LIVE registries
# (execution._strategies.SCALAR_HANDLERS and config._tables.GENERATE_TYPES),
# so adding a strategy or generation kind without cataloguing it fails.
# Each value is a DrawSite.draw_site_id or the DETERMINISTIC_NO_DRAW sentinel.
# ---------------------------------------------------------------------------

MASK_STRATEGY_TO_SITE: dict[str, str] = {
    "passthrough": DETERMINISTIC_NO_DRAW,
    "redact": DETERMINISTIC_NO_DRAW,
    "truncate": DETERMINISTIC_NO_DRAW,
    "bucketize": DETERMINISTIC_NO_DRAW,
    "top_code": DETERMINISTIC_NO_DRAW,
    "geo_generalize": DETERMINISTIC_NO_DRAW,
    "derived": DETERMINISTIC_NO_DRAW,
    "derived_aggregate": DETERMINISTIC_NO_DRAW,
    "text_redact": DETERMINISTIC_NO_DRAW,
    "nested": DETERMINISTIC_NO_DRAW,  # delegates to a child handler; no own draw
    "hash": "mask.hash",
    "tokenize": "mask.tokenize",
    "shuffle": "mask.shuffle",
    "categorical": "mask.categorical_deterministic",  # + mask.categorical_nondeterministic (seeded)
    "fpe": "mask.fpe",
    "date_shift": "mask.date_shift",
    "bucket_perturb": "mask.bucket_perturb",
    "group_key": "mask.group_key",
    "code_set": "mask.code_set",
    "joint_mask": "mask.joint_mask_keyed_row",
    "faker": "mask.faker",  # + mask.faker_nondeterministic (reuse, position-keyed)
    "formula": "mask.formula",
    "text_mask": "mask.text_mask_faker",  # + mask.text_mask_date_shift
    "grouped_series": "mask.grouped_series_monotone_walk",
    "windowed_date": "mask.windowed_date",
}

GEN_KIND_TO_SITE: dict[str, str] = {
    "sequence": DETERMINISTIC_NO_DRAW,
    "derived": DETERMINISTIC_NO_DRAW,
    "derived_aggregate": DETERMINISTIC_NO_DRAW,
    "group_key": "mask.group_key",  # group_key generation reuses the same derive()
    "faker": "gen.faker_per_row",
    "categorical": "gen.categorical",
    "reference": "gen.reference",
    "formula": "gen.formula_per_row",
    "statistical": "gen.statistical_per_row",
    "grouped_series": "mask.grouped_series_monotone_walk",
    "windowed_date": "mask.windowed_date",
}

# Synthetic-identifier providers (providers_v2). Cross-checked against the LIVE
# ProviderRegistry: the test enumerates every registered provider whose adapter
# is one of the identifier adapter classes and asserts the set equals these
# keys, so a new identifier adapter (a new derive_value / default_rng draw) fails
# until catalogued. Each has a deterministic (source_keyed_hmac) draw and a
# non-deterministic (unseeded default_rng) draw; the map points at the
# deterministic site, and gen.identifier_nondeterministic covers the other.
PROVIDER_IDENTIFIER_SITES: dict[str, str] = {
    "synthetic_ssn": "gen.identifier_deterministic",
    "synthetic_npi": "gen.identifier_deterministic",
    "synthetic_ein": "gen.identifier_deterministic",
    "synthetic_iban": "gen.identifier_deterministic",
    "synthetic_pan": "gen.identifier_deterministic",
    "synthetic_icd10": "gen.identifier_deterministic",
    "synthetic_mrn": "gen.identifier_deterministic",
    "synthetic_ndc": "gen.identifier_deterministic",
    "synthetic_cusip": "gen.identifier_deterministic",
}


def draw_site_by_id(draw_site_id: str) -> DrawSite:
    """Return the single ``DrawSite`` with ``draw_site_id`` (raises if absent)."""
    for site in DRAW_SITES:
        if site.draw_site_id == draw_site_id:
            return site
    raise KeyError(draw_site_id)


# Per-draw-site determinism providers (Task 0.3) live in a sibling module to
# keep this catalogue file focused on data. They are re-exported here so the
# protocol's public surface is one import. The import is at the bottom because
# the providers import DRAW_SITES / DrawSite / draw_site_by_id from this module;
# by this point those names are defined, so the cycle resolves cleanly.
from decoy_engine.execution.native._draw_site_providers import (  # noqa: E402
    DrawSiteProtocolError,
    DrawSiteProvider,
    all_providers,
    provider_for,
    unit_float_from_bits53,
)

__all__ = [
    "DETERMINISTIC_NO_DRAW",
    "DRAW_SITES",
    "ENTROPY_ROOTS",
    "FAMILIES",
    "GEN_KIND_TO_SITE",
    "IDENTITIES",
    "MASK_STRATEGY_TO_SITE",
    "PROVIDER_IDENTIFIER_SITES",
    "DrawSite",
    "DrawSiteProtocolError",
    "DrawSiteProvider",
    "all_providers",
    "draw_site_by_id",
    "provider_for",
    "unit_float_from_bits53",
]

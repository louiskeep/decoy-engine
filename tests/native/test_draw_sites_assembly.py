"""`DRAW_SITES` is exactly the per-family data modules concatenated in
catalogue order; the byte-level pin lives in test_draw_sites_catalogue_pinned.py."""

from __future__ import annotations

from decoy_engine.execution.native import _determinism_protocol as protocol
from decoy_engine.execution.native import _draw_site_types as types
from decoy_engine.execution.native._draw_sites_gen import GEN_DRAW_SITES, GEN_SUBSTRATE_DRAW_SITES
from decoy_engine.execution.native._draw_sites_gen_pool import GEN_POOL_DRAW_SITES
from decoy_engine.execution.native._draw_sites_mask import MASK_DRAW_SITES, MASK_FORMULA_DRAW_SITE
from decoy_engine.execution.native._draw_sites_provider_backed import PROVIDER_BACKED_DRAW_SITES
from decoy_engine.execution.native._draw_sites_tokenize import TOKENIZE_DRAW_SITES

_FAMILY_ORDER = (
    MASK_DRAW_SITES,
    GEN_DRAW_SITES,
    PROVIDER_BACKED_DRAW_SITES,
    GEN_SUBSTRATE_DRAW_SITES,
    (MASK_FORMULA_DRAW_SITE,),
    GEN_POOL_DRAW_SITES,
    TOKENIZE_DRAW_SITES,
)


def test_catalogue_is_the_family_modules_in_order() -> None:
    combined = tuple(site for family in _FAMILY_ORDER for site in family)
    assert len(protocol.DRAW_SITES) == len(combined)
    for index, (assembled, source) in enumerate(zip(protocol.DRAW_SITES, combined, strict=True)):
        assert assembled is source, index
    assert combined == protocol.DRAW_SITES


def test_family_sizes() -> None:
    assert [len(family) for family in _FAMILY_ORDER] == [14, 10, 4, 1, 1, 3, 1]


def test_protocol_reexports_the_record_type_and_enums() -> None:
    assert protocol.DrawSite is types.DrawSite
    assert protocol.FAMILIES is types.FAMILIES
    assert protocol.ENTROPY_ROOTS is types.ENTROPY_ROOTS
    assert protocol.IDENTITIES is types.IDENTITIES
    assert protocol.DETERMINISTIC_NO_DRAW is types.DETERMINISTIC_NO_DRAW

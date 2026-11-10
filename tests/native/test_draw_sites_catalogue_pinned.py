"""Pins the DRAW_SITES catalogue byte for byte.

Determinism depends on the catalogue's exact contents AND order (providers and
the coverage maps index into it). The digests below were captured from the
catalogue before it was split into per-family data modules: the whole tuple's
repr, each element's repr, the ordered ids, and the coverage maps. Any edit to
an entry, a reordering, or a dropped/duplicated site fails here.
"""

from __future__ import annotations

import hashlib

from decoy_engine.execution.native import _determinism_protocol as protocol


def _digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def test_catalogue_length_and_order_are_unchanged() -> None:
    assert len(protocol.DRAW_SITES) == len(_ORDERED_IDS) == 38
    assert [site.draw_site_id for site in protocol.DRAW_SITES] == _ORDERED_IDS


def test_catalogue_repr_is_byte_identical() -> None:
    assert _digest(protocol.DRAW_SITES) == _CATALOGUE_DIGEST


def test_each_entry_is_byte_identical() -> None:
    assert [_digest(site) for site in protocol.DRAW_SITES] == _ENTRY_DIGESTS


def test_coverage_maps_and_enums_are_unchanged() -> None:
    assert _digest(protocol.MASK_STRATEGY_TO_SITE) == _MAP_DIGESTS["MASK_STRATEGY_TO_SITE"]
    assert _digest(protocol.GEN_KIND_TO_SITE) == _MAP_DIGESTS["GEN_KIND_TO_SITE"]
    assert _digest(protocol.PROVIDER_IDENTIFIER_SITES) == _MAP_DIGESTS["PROVIDER_IDENTIFIER_SITES"]
    assert _digest(sorted(protocol.FAMILIES)) == _MAP_DIGESTS["FAMILIES"]
    assert _digest(sorted(protocol.ENTROPY_ROOTS)) == _MAP_DIGESTS["ENTROPY_ROOTS"]
    assert _digest(sorted(protocol.IDENTITIES)) == _MAP_DIGESTS["IDENTITIES"]
    assert _digest(protocol.DETERMINISTIC_NO_DRAW) == _MAP_DIGESTS["DETERMINISTIC_NO_DRAW"]


def test_lookup_by_id_returns_the_catalogued_entry() -> None:
    for site in protocol.DRAW_SITES:
        assert protocol.draw_site_by_id(site.draw_site_id) is site


# Re-pinned at the GEN-C1 main merge: carries the shuffle and derive_context re-pins plus the moved gen call sites.
# Re-pinned 2026-11-09: gen.reference records the fitted fan-out shuffles on its stream.
# Re-pinned 2026-11-09: gen.statistical_per_row records the fitted cell format draws.
# Re-pinned for GEN-UNIQUE E2: four unique-draw sites, the moved synthesize.py call sites, and the shared attempt limit.
_CATALOGUE_DIGEST = "44b38b3ab3d012ed5cef3ce0ae36fec7ba11bdd3d80e7049bf5c76f137a33208"

_ORDERED_IDS = [
    "mask.shuffle",
    "mask.hash",
    "mask.categorical_deterministic",
    "mask.categorical_nondeterministic",
    "mask.fpe",
    "mask.date_shift",
    "mask.bucket_perturb",
    "mask.group_key",
    "mask.code_set",
    "mask.joint_mask_keyed_row",
    "mask.text_mask_date_shift",
    "mask.text_mask_faker",
    "mask.grouped_series_monotone_walk",
    "mask.windowed_date",
    "gen.faker_per_row",
    "gen.categorical",
    "gen.reference",
    "gen.formula_per_row",
    "gen.statistical_per_row",
    "gen.null_probability",
    "gen.distribution_snapshot",
    "gen.pool_deterministic",
    "gen.pool_nondeterministic",
    "gen.composite_build_pool",
    "gen.faker_unique_retry",
    "gen.categorical_unique",
    "gen.reference_unique",
    "gen.unique_pair",
    "mask.faker",
    "gen.pool_build_faker",
    "gen.identifier_deterministic",
    "gen.identifier_nondeterministic",
    "gen.derive_context",
    "mask.formula",
    "gen.faker_pool_build",
    "gen.faker_pool_selection",
    "mask.faker_nondeterministic",
    "mask.tokenize",
]

_ENTRY_DIGESTS = [
    # mask.shuffle re-pinned 2026-11-08: the unkeyed mode draws from the mask key on the shared
    # (table, column) draw identity.
    "84b2887fc0363d71c893d6ad99f3d7026d891ee308091c68405db6dca943afb0",
    "8dce6622cbd27e05cc798e8b209a715c78834c056613f6adefced790bc4dfe9b",
    "33858d620973b39724613d7569dc732db3707e729236eeef8754518c1dfcf87a",
    "8ba3e20fb12b8a29cf52e18ef4d41d6c691bad77c6ac10a4651620d351f2a0c1",
    "6a3cc55998341cb8f0b3934d624c84e58a87555c701981ad8ee7d8030261221c",
    "8371d2808d3c97ce91ab93522edfa7459cb67b6f928e0cbfc6527d07e5e4a339",
    "d07b906a5a666ef5ae4239b27ee2a2bc3097872e1198f29716377d36d8be8ba3",
    "acb11366c3ae3b8fa817660874102ad3a6f377c92ba35ed73cbc007b80056bb5",
    "eb568e1842f6b938756adb3e6bb841b3633d04587dd9c8473a7312aa586d53ef",
    "bddaa1d1f3acf2a49832f1742b8163425877c835b2db5e47e76a97500f6893bb",
    "85be9f4b05fadf9b441f28ce151d9f820c48d99112fe21d799c4a759bc9f9bcd",
    "474ab1f76ab596a309f6b3f4b6a1e4995555c6f07473a831af12862a22283a60",
    "d30d45fdcacb74a57114f50fb09d3557bfe838db4afa0b1910f59816dc18b90b",
    "f01f96cde92e4a13757bc0724ba4eda31b1178bbb420edb144e676a144444b3c",
    "87297d66f6e12f6f2e9d18d0d9a66d2416e85d7901edc911ad2796d4fe85210b",
    "73d56b8732a6a41baad045d754751bf6b3952c717c042d8674f22c248e289980",
    # gen.reference re-pinned 2026-11-09: the fan-out allocator is a mirror call site.
    "ac3c610bf678df5df95d6723d19d382b3345640afddac78c71f3dfe2db3b2a97",
    "22ddc02bdf5fbcd992e9c621d629885b7c036e872f7f71b90f73fc3fa4f2023d",
    "e4ca36f5c5bfef93d57ebd502d2d24db01dac1f576643e83291f9a589c76f3c9",
    # gen.statistical_per_row re-pinned 2026-11-09: fitted cell_format adds the blank and null draws.
    "4dc3c2fce34978f9c0bfc138b7d17d375e4c852c520f836f50f95188145dd692",
    "fa2fd07097a3ed68edb71e46b40c43e0d697b50cf784b740b49c180bcae2e5d2",
    "451e4aa4bcb7f23e437468f4e692768c4571e4defb8a081d879be0eb428a1308",
    "648d1fb0b894493e6e8ad55de08b481dbbece29402ecb9ae3eb9cc54b995b211",
    "00cc3c083188a7299b963006aa4a5981a1abc2f0aee6e91e7ef31fe69cd532d4",
    "039d679f351c8e7d8e9e92ae0b5ee7e0cfa058cf99e1a71b409fc47327565a4a",
    "1dc06347f9b91e1fbbfcb307edfb7f7fea1c6c88e96c892c14a95af48e19d679",
    "b22f3004c978c61af573fd3bfaad6fb85dfddbc68e8a9a851c17173847b8e6cb",
    "890db69a3b481dbf7dd7ba338e26342350d8f073f3a5c6373b0449b3a0c5d206",
    "8a782e44afd97fb81aa7bbcc9acbf82e446ff3ef0cc19b7d2a0d3f99091e69ed",
    "4448026c1f789d220610a85dbcb0c17d8ce04e4497d1cda09e0346f713f5d2ba",
    "b9489ca4978756b4e41678489ce88c9f434a61526272c9cb639bbaddd282f944",
    "73e3c42955d797409d090d70e5b1c66d05ad12bd67ee820bc0f93b00a9b48970",
    "0e43e34c1059a22096de913327b9f7889ae6316c165037df78ec98b7c01598e1",
    "eeccee566ac297f0c47f8a3e8da4309688e3c6a307af233ad06158b3aedae08c",
    "d4a795b3273175d0c76a88f69cd8e3d97326497538694faa60b0886b0868bb02",
    "46b422be131d1e91c5ed2a6174d86f970429577b88e5da660ab3a4926171dab4",
    "f698d376bd1e62f5e3f17bde97a7e4da236bf76ad09baf49d95a8c6445614c2c",
    "c8ae317b9397f7faf9152ddc3c0e4e0d0fd55de2f87148512d76b9e28a343324",
]

_MAP_DIGESTS = {
    "MASK_STRATEGY_TO_SITE": "e42b340fc017f0608c08ec988fb697bb52c84f5cbb657dfa99b9531cd56681e9",
    "GEN_KIND_TO_SITE": "f5311cdfc5e5d172e6c63f12a15a79c1cb4480577f571d12fe391a94bbc6c1e1",
    "PROVIDER_IDENTIFIER_SITES": "19593ad7e4ac52025cdc024aa16bdb04ab776755b83e6f7c0e546210b6fc7cd6",
    "FAMILIES": "cbbf3f70f90b3995c7bb16f11bf1560ce135af11761cc6444aa41feaf659e811",
    "ENTROPY_ROOTS": "4f3fdb974258ed94e92188bd4dd456363696dba2d2a4a90a4f744c53879949f8",
    "IDENTITIES": "802762f296f31c9788f734245459e4480e13ea2a69c20cd43ab0c54b161a558a",
    "DETERMINISTIC_NO_DRAW": "1700d4ac7218adaf8022c7d6b1cd3424bf8b29871c28b67a312d9f07064e745e",
}

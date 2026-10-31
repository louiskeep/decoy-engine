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
    assert len(protocol.DRAW_SITES) == len(_ORDERED_IDS) == 34
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


_CATALOGUE_DIGEST = "63fe849ac714a1ea884189cd7959881c7aa7d524b8674117723de238a6e98fdd"

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
    "a4ee43513bc22f1c60f32ee3188c4fbb6f6db3ab51f95563109516378dc97217",
    "8dce6622cbd27e05cc798e8b209a715c78834c056613f6adefced790bc4dfe9b",
    "5510ffc7064f973e74e379c616a9284a98d40733ce8d92ee9a678556c88a2efd",
    "fe84b07488e2c883de51aa6f890ba06a6d0459fa055829d2821792907a45441d",
    "82ce53eee4841d4d5233ad441ef1f1bc8b12083bf3f2a486edafe1a57ebb62a6",
    "64ef8dd18d4b4d94c602d95b0565182b19d0b7635c02fadb8e6e2c1e107be284",
    "4fe7d8d2827dee249187a798cb82ddacbb8bbf0c6138a8cc55b3d10253425119",
    "acb11366c3ae3b8fa817660874102ad3a6f377c92ba35ed73cbc007b80056bb5",
    "eb568e1842f6b938756adb3e6bb841b3633d04587dd9c8473a7312aa586d53ef",
    "bddaa1d1f3acf2a49832f1742b8163425877c835b2db5e47e76a97500f6893bb",
    "85be9f4b05fadf9b441f28ce151d9f820c48d99112fe21d799c4a759bc9f9bcd",
    "474ab1f76ab596a309f6b3f4b6a1e4995555c6f07473a831af12862a22283a60",
    "d30d45fdcacb74a57114f50fb09d3557bfe838db4afa0b1910f59816dc18b90b",
    "f01f96cde92e4a13757bc0724ba4eda31b1178bbb420edb144e676a144444b3c",
    "dc05e03d1c4fda601fce54688971507cd1bfc189aa10646062b315aa9f4913c9",
    "b411c70c271961691308e0ea9318cce369107d1690f9b09d03046bb6aa436126",
    "793bc8be484782aa773cbe6556785f5f02619893e768996dc515fa820e6881a7",
    "0c45409c0a1dd96459b4ba69c4382899ffd0cebada23500d1b3491d13c76e4e3",
    "d45ba5c6dc53660b4cf3baa43e2faff146d2e4f91e0acd7fd977f731f3b05fe7",
    "d6e7cb8e9959f4ac1641affe43887f32a73edf5537d20bdf05680cdc3f4f0f01",
    "fa2fd07097a3ed68edb71e46b40c43e0d697b50cf784b740b49c180bcae2e5d2",
    "451e4aa4bcb7f23e437468f4e692768c4571e4defb8a081d879be0eb428a1308",
    "648d1fb0b894493e6e8ad55de08b481dbbece29402ecb9ae3eb9cc54b995b211",
    "00cc3c083188a7299b963006aa4a5981a1abc2f0aee6e91e7ef31fe69cd532d4",
    "134293fc3c9833886daacbd285fdc21443e7524062191d2e9951e966994238ea",
    "4448026c1f789d220610a85dbcb0c17d8ce04e4497d1cda09e0346f713f5d2ba",
    "b9489ca4978756b4e41678489ce88c9f434a61526272c9cb639bbaddd282f944",
    "73e3c42955d797409d090d70e5b1c66d05ad12bd67ee820bc0f93b00a9b48970",
    "ca03675f3cdc12192cf9e6bb08acad38c41841b85f4b05bf36a97fd202a656d9",
    "eeccee566ac297f0c47f8a3e8da4309688e3c6a307af233ad06158b3aedae08c",
    "d4a795b3273175d0c76a88f69cd8e3d97326497538694faa60b0886b0868bb02",
    "46b422be131d1e91c5ed2a6174d86f970429577b88e5da660ab3a4926171dab4",
    "54e2253c7d6a1d4c6cef9434aa7f18cbefd978b2e651db9778dea3e81ee923cc",
    "74b05a41097e9ccbfae3c63c74b2758557087333f1cfd1fe8796d070b0d050ea",
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

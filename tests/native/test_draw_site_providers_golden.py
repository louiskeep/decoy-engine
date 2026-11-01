"""Golden outputs for every registered draw-site provider.

Each scenario drives one provider's draw methods with fixed inputs and the
result is pinned by sha256 of its repr, captured before the providers moved
into the `native/protocol/` package. Also pins, per site, the provider class,
the partitionable flag and the outcome of a partition request, so a move that
re-registers, re-parents or re-wires a provider fails here.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from typing import Any

import pytest

from decoy_engine.determinism import IdentityDomain
from decoy_engine.execution.native._determinism_protocol import (
    DrawSiteProtocolError,
    all_providers,
    provider_for,
    unit_float_from_bits53,
)
from decoy_engine.generators.derivation import GenDeriveContext

_MK = bytes.fromhex("0102030405060708")
_JOB = bytes.fromhex("1122334455667788")
_NS = "col/demo"


def _ctx() -> GenDeriveContext:
    cfg = {"name": "c", "type": "faker"}
    return GenDeriveContext.for_column(derive_key=None, column_config=cfg, fallback_seed=42)


class _FakeFaker:
    """Deterministic stand-in: `seed_instance` reseeds, provider calls draw from it."""

    def __init__(self) -> None:
        self._rng = random.Random(0)

    def seed_instance(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def word(self) -> str:
        return f"w{self._rng.randrange(10**6)}"


def _np(gen: Any) -> list[float]:
    return [round(float(x), 12) for x in gen.random(4)]


def _faker_run(site: str) -> Any:
    faker = _FakeFaker()
    return provider_for(site).run(faker, faker.word, _ctx(), 3)


_SCENARIOS: dict[str, Callable[[], Any]] = {
    "mask.shuffle": lambda: (
        provider_for("mask.shuffle").seed_int(_MK, _NS, "c"),
        provider_for("mask.shuffle").permutation(_MK, _NS, "c", 7).tolist(),
    ),
    "mask.hash": lambda: provider_for("mask.hash").draw(_MK, _NS, b"alice").hex(),
    "mask.categorical_deterministic": lambda: provider_for("mask.categorical_deterministic").draw(
        _MK, _NS, b"alice", pool_size=11
    ),
    "mask.categorical_nondeterministic": lambda: provider_for(
        "mask.categorical_nondeterministic"
    ).draw(_MK, _NS, b"\x00\x01", pool_size=5),
    "mask.fpe": lambda: provider_for("mask.fpe").column_key(_MK, _NS).hex(),
    "mask.date_shift": lambda: provider_for("mask.date_shift").draw(_MK, _NS, b"2020").hex(),
    "mask.bucket_perturb": lambda: provider_for("mask.bucket_perturb").draw(_MK, _NS, b"7").hex(),
    "mask.group_key": lambda: provider_for("mask.group_key").draw(_MK, _NS, b"g").hex(),
    "mask.code_set": lambda: (
        provider_for("mask.code_set").hmac_key(_MK, None).hex(),
        provider_for("mask.code_set").select_index(_MK, _NS, "K10", 9),
        provider_for("mask.code_set").resolve_hole(4, 2),
        provider_for("mask.code_set").resolve_hole(1, 2),
    ),
    "mask.joint_mask_keyed_row": lambda: (
        provider_for("mask.joint_mask_keyed_row").hmac_key(_MK, _NS).hex(),
        provider_for("mask.joint_mask_keyed_row").row_index(_MK, _NS, "k", 13),
    ),
    "mask.text_mask_date_shift": lambda: provider_for("mask.text_mask_date_shift").shift_days(
        _MK, "2001-01-01", -30, 30
    ),
    "mask.text_mask_faker": lambda: (
        provider_for("mask.text_mask_faker").span_seed(_MK, "John Smith"),
        provider_for("mask.text_mask_faker").run(_FakeFaker(), "word", _MK, "John Smith"),
    ),
    "mask.grouped_series_monotone_walk": lambda: provider_for(
        "mask.grouped_series_monotone_walk"
    ).walk(_MK, _NS, "grp", 5, start=10, step=1, max_step=4),
    "mask.windowed_date": lambda: (
        provider_for("mask.windowed_date").row_seed_int(_MK, _NS, 3),
        _np(provider_for("mask.windowed_date").row_generator(_MK, _NS, 3)),
    ),
    "gen.faker_per_row": lambda: (
        provider_for("gen.faker_per_row").row_seed(_ctx(), 2),
        _faker_run("gen.faker_per_row"),
    ),
    "gen.categorical": lambda: provider_for("gen.categorical").choices(
        _ctx(), ["a", "b", "c"], [0.5, 0.3, 0.2], 6
    ),
    "gen.reference": lambda: [
        provider_for("gen.reference").sample(_ctx(), [1, 2, 3, 4], 6, distribution=d, weights=w)
        for d, w in (("random", None), ("sequential", None), ("weighted", [4, 3, 2, 1]))
    ],
    "gen.formula_per_row": lambda: provider_for("gen.formula_per_row").partitioned_draw(_ctx(), 3),
    "gen.statistical_per_row": lambda: provider_for("gen.statistical_per_row").partitioned_draw(
        100, 5
    ),
    "gen.null_probability": lambda: [
        round(float(x), 12) for x in provider_for("gen.null_probability").null_floats(_ctx(), 4)
    ],
    "gen.distribution_snapshot": lambda: _np(
        provider_for("gen.distribution_snapshot").col_generator(_ctx())
    ),
    "gen.pool_deterministic": lambda: provider_for("gen.pool_deterministic").draw(
        _JOB, _NS, b"cfg", pool_size=17
    ),
    "gen.pool_nondeterministic": lambda: _np(
        provider_for("gen.pool_nondeterministic").generator(b"\x00\x01\x02\x03")
    ),
    "gen.composite_build_pool": lambda: (
        _np(provider_for("gen.composite_build_pool").generator(b"\x09\x08")),
        _np(provider_for("gen.composite_build_pool").generator(None)),
    ),
    "mask.faker": lambda: provider_for("mask.faker").draw(_MK, _NS, b"alice", pool_size=23),
    "gen.pool_build_faker": lambda: (
        provider_for("gen.pool_build_faker").pool_seed(_JOB, "first_name", None, None, "h").hex(),
        provider_for("gen.pool_build_faker").faker_seed_int(_JOB, "city", "en_US", "ns", "h"),
    ),
    "gen.identifier_deterministic": lambda: repr(
        provider_for("gen.identifier_deterministic").draw(
            _JOB, _NS, b"row-1", domain=IdentityDomain()
        )
    ),
    "gen.identifier_nondeterministic": lambda: (
        type(provider_for("gen.identifier_nondeterministic").fresh_generator()).__name__
    ),
    "gen.derive_context": lambda: (
        provider_for("gen.derive_context").base_int(_ctx(), "np"),
        provider_for("gen.derive_context").row_int(_ctx(), "py", 4),
    ),
    "mask.formula": lambda: (
        provider_for("mask.formula").formula_seed("c", "value + 1"),
        provider_for("mask.formula").rng("c", "value + 1").random(),
    ),
    "gen.faker_pool_build": lambda: provider_for("gen.faker_pool_build").build_seed(_ctx()).hex(),
    "gen.faker_pool_selection": lambda: _np(
        provider_for("gen.faker_pool_selection").generator_for_column(_ctx())
    ),
    "mask.faker_nondeterministic": lambda: provider_for("mask.faker_nondeterministic").draw(
        _JOB, _NS, b"\x00\x02", pool_size=19
    ),
    "mask.tokenize": lambda: provider_for("mask.tokenize").draw(_MK, _NS, b"alice").hex(),
}


def _digest(value: Any) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _partition_outcome(site: str) -> str:
    provider = provider_for(site)
    try:
        provider.assert_partitionable()
    except DrawSiteProtocolError as exc:
        return f"refused:{exc.code}"
    return "allowed"


def test_every_registered_site_has_a_scenario() -> None:
    assert sorted(_SCENARIOS) == sorted(all_providers())


@pytest.mark.parametrize("site", sorted(_SCENARIOS))
def test_provider_output_matches_golden(site: str) -> None:
    assert _digest(_SCENARIOS[site]()) == _GOLDEN_OUTPUT[site]


def test_registry_shape_is_unchanged() -> None:
    shape = {
        site: (type(p).__name__, p.partitionable, _partition_outcome(site))
        for site, p in sorted(all_providers().items())
    }
    assert _digest(shape) == _GOLDEN_REGISTRY


def test_unit_float_known_answers() -> None:
    assert unit_float_from_bits53(0) == 0.0
    assert unit_float_from_bits53((1 << 64) - 1) == (2**53 - 1) / 2**53


_GOLDEN_OUTPUT: dict[str, str] = {
    "gen.categorical": "96e246f7f3c04be5ad8641ad9e01fc9c78de67e2a6227e52d371a7ee93ca14e9",
    "gen.composite_build_pool": "e804c947fbca8cbce1e13b3f6c5860e7050f6434e2b1c37f4a0278d080f521ce",
    "gen.derive_context": "20ec3a1cb72511b568bd093218c7489fb210bc14a0479f0219efcf80898cd6ae",
    "gen.distribution_snapshot": "5daeda46ebb57fa45f1dbb2276be2f565e8a188faeaebb441c0aa089acb25b0d",
    "gen.faker_per_row": "17d38be75697a9417e6418af8da9a533107bc496cfc85dd3fba44404e5593cb2",
    "gen.faker_pool_build": "056a774aebaf733f270117a03dcfa6f79b53c8475a2bddec6cc5807fe31f719a",
    "gen.faker_pool_selection": "a7c52332f2a0d24e3a0586faa1d7ad6a3857007098c89932eb6fa7e215de3876",
    "gen.formula_per_row": "ec4bf4b74327f8b5bba95d65de52a58950b5266a33824c970aaf6b03936ddd69",
    "gen.identifier_deterministic": "81a00a6fdfd16336fdc5364bf6eb4412dcd8b450fbab27ff544462b38d20902e",
    "gen.identifier_nondeterministic": "f45f9e4a39ae8d18062a4e51187b3ca9a58f706ce2380c2bb57bd2c86d5887b6",
    "gen.null_probability": "5daeda46ebb57fa45f1dbb2276be2f565e8a188faeaebb441c0aa089acb25b0d",
    "gen.pool_build_faker": "5647a06090b261df876dbeb77d0318bcf2638877dc76093c6806fd69c117873f",
    "gen.pool_deterministic": "19581e27de7ced00ff1ce50b2047e7a567c76b1cbaebabe5ef03f7c3017bb5b7",
    "gen.pool_nondeterministic": "7f41927c15f30adce869369f6503fa9ba3a706d26fa51625361defa621e3576e",
    "gen.reference": "614decc84823bcc916d47f007076d5dedcc8edb1eb28270e5f484e708aadedd7",
    "gen.statistical_per_row": "1253e9373e781b7500266caa55150e08e210bc8cd8cc70d89985e3600155e860",
    "mask.bucket_perturb": "237f537111d6747ea6ff642ca6ab18ff88f6936e45e993c52f4c2d42dc18eb9d",
    "mask.categorical_deterministic": "e7f6c011776e8db7cd330b54174fd76f7d0216b612387a5ffcfb81e6f0919683",
    "mask.categorical_nondeterministic": "5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9",
    "mask.code_set": "39a5ea421300c982f5be9f92e7d0116858035a23de91be98ac64d9ab5a45afd3",
    "mask.date_shift": "6d09a52becbc2be47b141fbaef6417e684f1d543def38adbb356d9d1af1d78dc",
    "mask.faker": "ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d",
    "mask.faker_nondeterministic": "4e07408562bedb8b60ce05c1decfe3ad16b72230967de01f640b7e4729b49fce",
    "mask.formula": "8a2485547325a73e8b0837a111c526674f9fc24c8d416534b866d5d9c9513fc8",
    "mask.fpe": "aefa0682ed1d2c6c5805b325fa89461786fcbee38277f00d544a1b201fbd07e6",
    "mask.group_key": "91a8c3eee175ca44a828249240e4abde92559c0cddf6bec7c6773929406e7d65",
    "mask.grouped_series_monotone_walk": "fb2192725ef26f254b1124356a13d36ece77d43e3051d4bbf1b889e99b3555c1",
    "mask.hash": "b931e4cddb5420a92e65ac3c8a1368bcf04c2d1724787ff94053377dd97fd2be",
    "mask.joint_mask_keyed_row": "0ca1fad82ed7cb5c6c40aeb27bfd029d82c3282152468d03f45cb82da746cf8d",
    "mask.shuffle": "ec0fb76703bb6a80cc8ce607aefcca3941652855b79c73cad5d3b678235d0b57",
    "mask.text_mask_date_shift": "4a44dc15364204a80fe80e9039455cc1608281820fe2b24f1e5233ade6af1dd5",
    "mask.text_mask_faker": "eef779676d353b92d67b9797c6b3c3b020185d961487ae78af44ca7981ab8489",
    "mask.tokenize": "b931e4cddb5420a92e65ac3c8a1368bcf04c2d1724787ff94053377dd97fd2be",
    "mask.windowed_date": "e7155d227e2e065ea4ad6de21ddd5f12c08f5d6a9654351f208c5f6f7b2b612a",
}

_GOLDEN_REGISTRY = "84d747bd4d004f73a8035c620f3d83d392560bb4f2a8ecdaf4f8b826ce9817ff"

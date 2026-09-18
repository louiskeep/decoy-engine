"""Faker-widening ADDITION slice: `pool_eligible`'s locale-aware gate.

Covers the plan's VERIFY item 1 (`pool_eligible` unit coverage) and item 2's
exact-set half (`POOL_ELIGIBLE_LOCALE_PAIRS` == the harness's filtered-new
135). The None-vs-en_US identity claim (Codex-2) is tested at the
`build_pool_values` seam, not by comparing two `_v2_run` column configs --
`locale` participates in `strategy_config_fingerprint`, so two configs that
differ only in an explicit `"en_US"` vs an absent `locale` key derive
different seed roots and are not expected to produce identical columns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decoy_engine.generation import _faker_pool
from decoy_engine.generators.derivation import GenDeriveContext

ENGINE_ROOT = Path(__file__).resolve().parents[3]
CERTIFIED_PAIRS_PATH = ENGINE_ROOT / "scripts" / "faker-determinism" / "certified_pairs.json"

# A spread across types and locales, not just the first few entries -- enough
# to catch an off-by-one in the literal transcription without asserting all
# 135 twice (the exact-set test below already does that in full).
_SAMPLE_CERTIFIED_PAIRS = [
    ("administrative_unit", "en_US"),
    ("bs", "fr_FR"),
    ("catch_phrase", "de_DE"),
    ("city_prefix", "en_GB"),
    ("city_suffix", "es_ES"),
    ("company_suffix", "en_US"),
    ("country_code", "fr_FR"),
    ("current_country", "de_DE"),
    ("first_name_female", "en_US"),
    ("first_name_male", "es_ES"),
    ("first_name_nonbinary", "en_GB"),
    ("job_female", "fr_FR"),
    ("job_male", "de_DE"),
    ("last_name_female", "en_US"),
    ("last_name_male", "en_GB"),
    ("last_name_nonbinary", "es_ES"),
    ("military_state", "en_US"),
    ("name_female", "fr_FR"),
    ("name_male", "de_DE"),
    ("name_nonbinary", "en_US"),
    ("prefix_female", "en_GB"),
    ("prefix_male", "es_ES"),
    ("prefix_nonbinary", "fr_FR"),
    ("state_abbr", "en_US"),
    ("street_name", "de_DE"),
    ("street_suffix", "en_US"),
    ("suffix_female", "en_GB"),
    ("suffix_male", "es_ES"),
    ("suffix_nonbinary", "fr_FR"),
]

_LEGACY_TYPES = sorted(_faker_pool.POOL_ELIGIBLE_FAKER_TYPES)


class TestPoolEligibleCertifiedPairs:
    @pytest.mark.parametrize("faker_type,locale", _SAMPLE_CERTIFIED_PAIRS)
    def test_certified_pair_eligible_at_threshold(self, faker_type: str, locale: str) -> None:
        assert _faker_pool.pool_eligible(
            faker_type, locale, _faker_pool.N_THRESHOLD, opted_out=False
        )

    def test_non_certified_locale_for_new_type_not_eligible(self) -> None:
        # first_name_female is certified for en_US/en_GB/de_DE/es_ES/fr_FR,
        # not ja_JP.
        assert not _faker_pool.pool_eligible(
            "first_name_female", "ja_JP", _faker_pool.N_THRESHOLD, opted_out=False
        )

    def test_non_certified_type_not_eligible(self) -> None:
        assert not _faker_pool.pool_eligible(
            "pyint", "en_US", _faker_pool.N_THRESHOLD, opted_out=False
        )

    def test_list_locale_for_new_type_declines_without_error(self) -> None:
        # `make_faker` accepts a locale list; `pool_eligible` must not raise
        # trying to hash it against a frozenset of (type, str) tuples.
        assert not _faker_pool.pool_eligible(
            "first_name_female", ["en_US", "de_DE"], _faker_pool.N_THRESHOLD, opted_out=False
        )

    def test_none_locale_for_new_type_resolves_to_en_us(self) -> None:
        # first_name_female/en_US is certified, so an absent locale (Faker's
        # own default) is eligible; a type with no en_US pair would not be.
        assert _faker_pool.pool_eligible(
            "first_name_female", None, _faker_pool.N_THRESHOLD, opted_out=False
        )

    def test_below_threshold_certified_pair_not_eligible(self) -> None:
        assert not _faker_pool.pool_eligible(
            "first_name_female", "en_US", _faker_pool.N_THRESHOLD - 1, opted_out=False
        )

    def test_opted_out_certified_pair_not_eligible(self) -> None:
        assert not _faker_pool.pool_eligible(
            "first_name_female", "en_US", _faker_pool.N_THRESHOLD, opted_out=True
        )


class TestPoolEligibleLegacyTypesUnaffected:
    """The 10 legacy types stay global across every locale shape -- no
    regression from adding the locale-scoped check (the legacy branch
    short-circuits before any locale is looked at)."""

    @pytest.mark.parametrize("faker_type", _LEGACY_TYPES)
    def test_legacy_type_eligible_in_en_us(self, faker_type: str) -> None:
        assert _faker_pool.pool_eligible(
            faker_type, "en_US", _faker_pool.N_THRESHOLD, opted_out=False
        )

    @pytest.mark.parametrize("faker_type", _LEGACY_TYPES)
    def test_legacy_type_eligible_in_ja_jp(self, faker_type: str) -> None:
        assert _faker_pool.pool_eligible(
            faker_type, "ja_JP", _faker_pool.N_THRESHOLD, opted_out=False
        )

    @pytest.mark.parametrize("faker_type", _LEGACY_TYPES)
    def test_legacy_type_eligible_with_list_locale(self, faker_type: str) -> None:
        assert _faker_pool.pool_eligible(
            faker_type, ["en_US", "ja_JP"], _faker_pool.N_THRESHOLD, opted_out=False
        )

    @pytest.mark.parametrize("faker_type", _LEGACY_TYPES)
    def test_legacy_type_eligible_with_none_locale(self, faker_type: str) -> None:
        assert _faker_pool.pool_eligible(faker_type, None, _faker_pool.N_THRESHOLD, opted_out=False)


def test_pool_eligible_locale_pairs_matches_harness_filtered_new_set() -> None:
    """Codex-3: the embedded literal must be EXACTLY the harness's certified
    pairs with `kwargs != {}` and the 10 legacy types filtered out -- not a
    superset or subset. An accidental extra or missing pair fails this,
    where "spot check a sample + one negative" would not."""
    certified = json.loads(CERTIFIED_PAIRS_PATH.read_text(encoding="utf-8"))
    expected = frozenset(
        (row["faker_type"], row["locale"])
        for row in certified
        if row["kwargs"] == {} and row["faker_type"] not in _faker_pool.POOL_ELIGIBLE_FAKER_TYPES
    )
    assert expected == _faker_pool.POOL_ELIGIBLE_LOCALE_PAIRS


def test_none_and_en_us_build_the_same_pool_under_the_same_seeds() -> None:
    """Codex-2: None resolving to Faker's en_US default is sound for
    ADMISSION, but two column configs (one omitting `locale`, one setting
    `"en_US"`) are not byte-identical columns -- `locale` feeds
    `strategy_config_fingerprint`, so they derive different seed roots.
    The identity claim only holds at the build seam, under the SAME
    `GenDeriveContext`/seeds: `make_faker(None)` and `make_faker("en_US")`
    must behave identically given the same seed_instance call."""
    gen_ctx = GenDeriveContext.for_column(
        derive_key=None,
        column_config={"name": "v", "type": "faker", "faker_type": "first_name_female"},
        fallback_seed=1,
    )
    build_seed = gen_ctx.family_bytes(_faker_pool._BUILD_FAMILY)[:8]

    values_none, exact_none, custom_none = _faker_pool.build_pool_values(
        "first_name_female", None, build_seed, 50, {}
    )
    values_en_us, exact_en_us, custom_en_us = _faker_pool.build_pool_values(
        "first_name_female", "en_US", build_seed, 50, {}
    )

    assert exact_none is True and custom_none is False
    assert exact_en_us is True and custom_en_us is False
    assert values_none == values_en_us

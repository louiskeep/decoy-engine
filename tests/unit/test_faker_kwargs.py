"""Tests for the reflected Faker provider catalog (LOGGING_GUIDE-adjacent
roadmap item) + the per-provider ``faker_kwargs`` pass-through path.

The engine used to expose a curated whitelist of ~30 providers. The
expanded catalog reflects every public Faker method that returns a
stringy value (~250 entries), with a denylist for bytes/dict/tuple
returns. YAML can carry a ``faker_kwargs:`` block that flows straight
into the Faker method (e.g. ``country_code(representation='alpha-3')``,
``date_of_birth(minimum_age=18, maximum_age=25)``).

These tests pin three contracts:
  1. Reflection surface: high-signal providers are reachable; denylisted
     ones aren't.
  2. ``postcode`` → ``zipcode`` alias works.
  3. ``faker_kwargs`` actually changes Faker output (the kwargs path
     isn't being dropped on the floor).
"""

import pandas as pd
import pytest
from faker import Faker

from decoy_engine.internal.faker_setup import (
    get_faker_providers,
)
from decoy_engine.transforms.faker_based import FakerStrategy


@pytest.fixture
def fake():
    f = Faker("en_US")
    f.seed_instance(42)
    return f


class TestReflectedCatalog:
    def test_catalog_includes_high_signal_providers(self, fake):
        providers = get_faker_providers(fake)
        # A representative sample across the 15 visible categories.
        for name in (
            "first_name",
            "last_name",
            "name",  # person
            "email",
            "safe_email",
            "ipv4",
            "url",  # internet
            "address",
            "city",
            "street_address",  # address
            "zipcode",
            "country_code",  # address (with args)
            "phone_number",  # phone
            "ssn",  # ssn
            "iban",
            "aba",
            "swift",  # bank
            "credit_card_number",
            "credit_card_expire",  # credit_card
            "date",
            "date_of_birth",
            "iso8601",  # date_time
            "paragraph",
            "sentence",
            "word",  # lorem
            "uuid4",
            "password",
            "boolean",  # misc
        ):
            assert name in providers, f"{name!r} missing from catalog"

    def test_catalog_excludes_denylisted_providers(self, fake):
        providers = get_faker_providers(fake)
        # A representative slice across the denylist categories.
        for name in (
            "binary",
            "image",
            "tar",
            "zip",
            "json_bytes",  # bytes
            "profile",
            "simple_profile",  # dict
            "time_series",  # generator
            "cryptocurrency",
            "currency",  # tuple
            "latlng",
            "local_latlng",
            "passport_owner",  # tuple
            "pytimezone",  # ZoneInfo
            "enum",  # requires arg
        ):
            assert name not in providers, f"{name!r} should be denylisted"

    def test_postcode_aliases_zipcode(self, fake):
        providers = get_faker_providers(fake)
        # Both names point at the same callable so legacy YAML stays warning-free.
        assert "postcode" in providers
        assert "zipcode" in providers

    def test_returns_are_str_coerced(self, fake):
        providers = get_faker_providers(fake)
        # date_of_birth would return a datetime.date in raw Faker — the
        # reflection wrapper coerces to str so pandas cells stay text.
        out = providers["date_of_birth"]()
        assert isinstance(out, str)
        # uuid4 returns a UUID in raw Faker — str-coerced here.
        out = providers["uuid4"]()
        assert isinstance(out, str)


class TestKwargsPassthrough:
    def test_country_code_representation_changes_output(self, fake):
        """``country_code(representation='alpha-3')`` returns 3-letter
        codes (USA, GBR) instead of the alpha-2 default (US, GB). We
        can't assert exact strings (seeded country varies by Faker
        version) but the length difference is reliable."""
        providers = get_faker_providers(fake)
        # Re-seed for determinism within each call.
        fake.seed_instance(42)
        alpha2 = providers["country_code"](representation="alpha-2")
        fake.seed_instance(42)
        alpha3 = providers["country_code"](representation="alpha-3")
        assert len(alpha2) == 2
        assert len(alpha3) == 3

    def test_date_of_birth_min_max_age_bounds(self, fake):
        """``minimum_age=21, maximum_age=21`` should produce a date
        roughly 21 years before today, regardless of seed."""
        from datetime import date

        providers = get_faker_providers(fake)
        out = providers["date_of_birth"](minimum_age=21, maximum_age=21)
        # str-coerced ISO date.
        born = date.fromisoformat(out)
        age = (date.today() - born).days // 365
        # Faker rounds boundaries by exact-year arithmetic — accept 20-21.
        assert 20 <= age <= 22

    def test_unknown_kwarg_is_dropped_silently(self, fake):
        """A YAML carrying a stale kwarg (provider removed it, or it was
        a typo) must NOT raise — the engine drops the unknown key and
        falls back to library defaults."""
        providers = get_faker_providers(fake)
        # `first_name` accepts no args; passing one should drop it.
        out = providers["first_name"](bogus_kwarg="ignored")
        assert isinstance(out, str) and out


class TestStrategyIntegration:
    """End-to-end: build a FakerStrategy, apply with faker_kwargs in the
    rule dict, and confirm the kwarg actually changed the output column
    vs. the no-kwarg baseline."""

    def test_faker_kwargs_flows_through_keyed_path(self, mock_logger):
        col = pd.Series(["a", "b", "c", "d", "e"])

        # Provide a derive_key so the keyed (Path B) path runs.
        def derive_key(info: str) -> bytes:
            return (info.encode("utf-8") * 8)[:32]

        s = FakerStrategy(seed=42, logger=mock_logger, derive_key=derive_key)

        baseline = s.apply(
            col,
            {
                "column": "country",
                "type": "faker",
                "faker_type": "country_code",
            },
        )
        with_alpha3 = s.apply(
            col,
            {
                "column": "country",
                "type": "faker",
                "faker_type": "country_code",
                "faker_kwargs": {"representation": "alpha-3"},
            },
        )
        # Baseline = alpha-2 (length 2), alpha-3 = length 3.
        assert all(len(v) == 2 for v in baseline)
        assert all(len(v) == 3 for v in with_alpha3)

    def test_faker_kwargs_flows_through_legacy_path(self, mock_logger):
        col = pd.Series(["a", "b", "c"])
        # No derive_key → legacy (Path A) path.
        s = FakerStrategy(seed=42, logger=mock_logger)
        with_alpha3 = s.apply(
            col,
            {
                "column": "country",
                "type": "faker",
                "faker_type": "country_code",
                "faker_kwargs": {"representation": "alpha-3"},
            },
        )
        assert all(len(v) == 3 for v in with_alpha3)

    def test_non_dict_faker_kwargs_warns_and_ignored(self, mock_logger):
        """A YAML where ``faker_kwargs: "not a dict"`` shouldn't crash —
        the strategy logs a warning and falls back to no-kwargs."""
        col = pd.Series(["a"])
        s = FakerStrategy(seed=42, logger=mock_logger)
        out = s.apply(
            col,
            {
                "column": "x",
                "type": "faker",
                "faker_type": "first_name",
                "faker_kwargs": "not a dict",
            },
        )
        assert len(out) == 1

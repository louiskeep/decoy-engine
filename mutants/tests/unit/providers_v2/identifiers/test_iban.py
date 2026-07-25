"""MG-1 S4 (2026-06-01): IBAN domain generator regression cells.

Pins mod-97 check digit computation + per-country length coverage
+ adapter contracts. The country list shared with
decoy_engine.storm.detectors._IBAN_COUNTRIES; the per-country
length table is the IBAN-specific risk Dennis flagged.
"""

from __future__ import annotations

import numpy as np
import pytest

from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2.identifiers._iban import (
    _COUNTRIES_ORDERED,
    _IBAN_LENGTH_BY_COUNTRY,
    IbanAdapter,
    IbanDomain,
    _compute_iban_check,
    _generate_iban_from_country_and_bban,
    _letter_to_digits,
    generate_random,
)
from decoy_engine.storm.detectors import _IBAN_COUNTRIES, _iban_valid

_SEED = (0x0123456789).to_bytes(8, "big")


def _spec(
    *, deterministic: bool = False, namespace: str | None = None, seed: bytes | None = None
) -> ProviderSpec:
    return ProviderSpec(
        locale="en_US",
        deterministic=deterministic,
        namespace=namespace,
        seed=seed,
    )


# ── Coverage ──────────────────────────────────────────────────────


class TestCountryCoverage:
    """The length table must cover every country in
    decoy_engine.storm.detectors._IBAN_COUNTRIES, since the detector
    rejects IBANs whose country code isn't in that set."""

    def test_length_table_covers_every_detector_country(self):
        missing = _IBAN_COUNTRIES - set(_IBAN_LENGTH_BY_COUNTRY.keys())
        assert missing == frozenset(), (
            f"Country codes in detector set but missing from length table: {sorted(missing)!r}"
        )

    def test_length_table_has_no_unknown_countries(self):
        extra = set(_IBAN_LENGTH_BY_COUNTRY.keys()) - _IBAN_COUNTRIES
        assert extra == set(), (
            f"Country codes in length table but not in detector set: {sorted(extra)!r}"
        )

    def test_every_length_is_in_iso_13616_range(self):
        # ISO 13616 caps at 34 characters total; the detector also
        # rejects below 15 characters.
        for country, length in _IBAN_LENGTH_BY_COUNTRY.items():
            assert 15 <= length <= 34, f"{country} length {length} outside ISO 13616 15-34 range"


# ── mod-97 algorithm ──────────────────────────────────────────────


class TestMod97:
    def test_letter_to_digits_a_through_z(self):
        # A=10 .. Z=35 per ISO 13616.
        assert _letter_to_digits("A") == "10"
        assert _letter_to_digits("Z") == "35"
        assert _letter_to_digits("DE") == "1314"
        # Digits pass through unchanged.
        assert _letter_to_digits("DE89") == "131489"

    def test_compute_check_matches_canonical_examples(self):
        # Canonical example from the ISO 13616 documentation:
        # DE89 3704 0044 0532 0130 00 -- the check digits are 89.
        check = _compute_iban_check("DE", "370400440532013000")
        assert check == "89"

    def test_generated_iban_passes_validator(self):
        # GB22 7006 0000 0010 0010 04 is a SWIFT canonical UK example;
        # we don't reproduce it but the generator must produce
        # validator-passing GB IBANs of length 22.
        iban = _generate_iban_from_country_and_bban("GB", "0" * 18)
        assert _iban_valid(iban)
        assert iban.startswith("GB")
        assert len(iban) == 22


# ── Domain (from_bytes) ───────────────────────────────────────────


class TestIbanDomain:
    def test_from_bytes_returns_valid_iban_across_seed_space(self):
        domain = IbanDomain()
        for seed_byte in range(0, 256, 7):
            b = bytes([seed_byte] * 32)
            iban = domain.from_bytes(b)
            assert _iban_valid(iban), (
                f"IbanDomain.from_bytes returned non-valid {iban!r} for byte seed {seed_byte!r}."
            )

    def test_from_bytes_deterministic(self):
        domain = IbanDomain()
        b = bytes([42] * 32)
        assert domain.from_bytes(b) == domain.from_bytes(b)

    def test_from_bytes_wrong_length_raises(self):
        domain = IbanDomain()
        from decoy_engine.providers_v2.identifiers._errors import IdentifierError

        with pytest.raises(IdentifierError):
            domain.from_bytes(b"x" * 16)

    def test_from_bytes_country_matches_table_length(self):
        """The total IBAN length matches the per-country entry."""
        domain = IbanDomain()
        for seed_byte in range(0, 256, 31):
            iban = domain.from_bytes(bytes([seed_byte] * 32))
            country = iban[:2]
            assert len(iban) == _IBAN_LENGTH_BY_COUNTRY[country], (
                f"IBAN {iban!r} has length {len(iban)} but country "
                f"{country} expects {_IBAN_LENGTH_BY_COUNTRY[country]}"
            )


# ── Random generator ──────────────────────────────────────────────


class TestRandomGenerator:
    def test_random_ibans_validate(self):
        rng = np.random.default_rng(seed=42)
        for _ in range(100):
            iban = generate_random(rng)
            assert _iban_valid(iban), f"Got invalid {iban!r}"

    def test_random_handles_max_length_country(self):
        """RU has BBAN length 29 (total 33). The generator builds
        the BBAN digit-by-digit since numpy int64 caps at 10**19;
        verify the max-length path works."""
        # Force RU via a custom rng that always picks the RU index.
        ru_idx = _COUNTRIES_ORDERED.index("RU")

        class _ForceRu:
            def integers(self, lo, hi):
                # The generator's first call selects country; rest
                # select digits. Return ru_idx for the first, 0 for
                # subsequent (digit-by-digit).
                if hi == len(_COUNTRIES_ORDERED):
                    return ru_idx
                return 0

        rng = _ForceRu()
        iban = generate_random(rng)  # type: ignore[arg-type]
        assert iban.startswith("RU")
        assert len(iban) == 33
        assert _iban_valid(iban)


# ── Adapter ───────────────────────────────────────────────────────


class TestIbanAdapter:
    def test_unknown_provider_rejected(self):
        adapter = IbanAdapter()
        with pytest.raises(ProviderError, match="unknown_provider"):
            adapter.generate("not_iban", spec=_spec())

    def test_random_generate(self):
        adapter = IbanAdapter()
        spec = _spec()
        for _ in range(10):
            iban = adapter.generate("synthetic_iban", spec=spec)
            assert _iban_valid(iban)

    def test_deterministic_same_input_same_output(self):
        adapter = IbanAdapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        out1 = adapter.generate("synthetic_iban", spec=spec, source_value="acct-001")
        out2 = adapter.generate("synthetic_iban", spec=spec, source_value="acct-001")
        assert out1 == out2
        assert _iban_valid(out1)

    def test_deterministic_different_input_different_output(self):
        adapter = IbanAdapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        out1 = adapter.generate("synthetic_iban", spec=spec, source_value="acct-001")
        out2 = adapter.generate("synthetic_iban", spec=spec, source_value="acct-002")
        assert out1 != out2

    def test_batch_size(self):
        adapter = IbanAdapter()
        spec = _spec()
        batch = adapter.generate_batch("synthetic_iban", spec=spec, count=15)
        assert len(batch) == 15
        for iban in batch:
            assert _iban_valid(iban)

    def test_batch_deterministic_unsupported(self):
        adapter = IbanAdapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        with pytest.raises(ProviderError, match="batch_deterministic_unsupported"):
            adapter.generate_batch("synthetic_iban", spec=spec, count=4)

    def test_capability_matrix(self):
        adapter = IbanAdapter()
        cap = adapter.capability_matrix("synthetic_iban")
        assert cap.provider == "synthetic_iban"
        assert cap.supports_deterministic is True
        assert "iban_mod97_check" in cap.blocklist_validators

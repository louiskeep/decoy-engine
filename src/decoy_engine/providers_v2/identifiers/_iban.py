"""IBAN identifier: International Bank Account Number per ISO 13616.

Format:
- Position 1-2: ISO 3166 country code (alpha-2).
- Position 3-4: 2 check digits.
- Position 5-N: BBAN (country-specific length + structure).

Validation:
- Length matches the country's BBAN length per the SWIFT IBAN registry.
- Country code in the IBAN-issuing country set.
- mod-97 check digits resolve to 1 (rearrange [BBAN + country + check]
  + letter-to-digit substitution).

SWIFT IBAN Registry source (the canonical per-country length table):
https://www.swift.com/standards/data-standards/iban-international-bank-account-number

Quarterly source review: the SWIFT registry adds new countries
occasionally (last big update added BY / IQ / EG / GT). The table
below is conservative + covers every country in
decoy_engine.storm.detectors._IBAN_COUNTRIES. Last reviewed:
2026-06-01.

MG-1 S4 (2026-06-01). Dennis explicitly budgeted IBAN as a full
eng-day because the per-country length table is the IBAN-specific
risk; the table below is the canonical SWIFT length set as of the
review date.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from decoy_engine.determinism import derive_value
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.providers_v2._adapter import CapabilityMatrix, ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2.identifiers._errors import IdentifierError
from decoy_engine.storm.detectors import _IBAN_COUNTRIES, _iban_valid


# Per-country IBAN total length (country + check + BBAN) per the
# SWIFT IBAN registry. Every country in _IBAN_COUNTRIES has an
# entry; the generator picks a country, looks up the length, and
# emits a BBAN of (length - 4) digits.
_IBAN_LENGTH_BY_COUNTRY: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20,
    "BE": 16, "BG": 22, "BH": 22, "BR": 29, "BY": 28, "CH": 21,
    "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28,
    "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
    "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26,
    "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32,
    "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24,
    "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18,
    "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29,
    "RO": 24, "RS": 22, "RU": 33, "SA": 24, "SC": 31, "SE": 24,
    "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23,
    "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}


# Stable ordered list of country codes for deterministic byte->country
# mapping. Sorted so the mapping is reproducible across releases.
_COUNTRIES_ORDERED: tuple[str, ...] = tuple(sorted(_IBAN_LENGTH_BY_COUNTRY.keys()))


def _letter_to_digits(s: str) -> str:
    """Map IBAN letters to their mod-97 digit equivalent: A=10..Z=35."""
    out: list[str] = []
    for ch in s:
        if ch.isdigit():
            out.append(ch)
        else:
            out.append(str(ord(ch.upper()) - ord("A") + 10))
    return "".join(out)


def _compute_iban_check(country: str, bban: str) -> str:
    """Compute the 2-digit check digits per ISO 13616.

    Algorithm: rearrange to BBAN + country + '00', substitute letters
    for digits, compute mod 97; check = 98 - mod. Zero-pad to 2 digits.
    """
    rearranged = bban + country + "00"
    numeric = _letter_to_digits(rearranged)
    check = 98 - (int(numeric) % 97)
    return f"{check:02d}"


def _generate_iban_from_country_and_bban(country: str, bban: str) -> str:
    """Combine country (2 letters) + 2-digit check + BBAN (digits)."""
    check = _compute_iban_check(country, bban)
    return country + check + bban


def _generate_iban_from_bytes(b: bytes) -> str:
    """Map 32 random bytes onto a valid IBAN:

    - country from b[0] mod country_count
    - BBAN digits from int.from_bytes(b[1:], 'big'), modulo
      10**bban_length, zero-padded
    - check digits computed from country + BBAN per ISO 13616
    """
    country_idx = b[0] % len(_COUNTRIES_ORDERED)
    country = _COUNTRIES_ORDERED[country_idx]
    total_length = _IBAN_LENGTH_BY_COUNTRY[country]
    bban_length = total_length - 4
    # 31 bytes is more than enough to fit any BBAN's digit count
    # (the longest BBAN is RU at 29 digits = ~12 bytes).
    bban_int = int.from_bytes(b[1:], "big") % (10 ** bban_length)
    bban = f"{bban_int:0{bban_length}d}"
    return _generate_iban_from_country_and_bban(country, bban)


@dataclass(frozen=True)
class IbanDomain:
    rng_config: dict[str, Any] | None = None

    def from_bytes(self, b: bytes) -> str:
        if len(b) != 32:
            raise IdentifierError(
                code="invalid_input_length",
                message=f"IbanDomain.from_bytes expects 32 bytes; got {len(b)}.",
            )
        return _generate_iban_from_bytes(b)


class IbanValidator:
    @staticmethod
    def is_valid(value: str) -> bool:
        return _iban_valid(value)


def generate_random(rng: np.random.Generator, locale: str = "en_US") -> str:
    country = _COUNTRIES_ORDERED[rng.integers(0, len(_COUNTRIES_ORDERED))]
    total_length = _IBAN_LENGTH_BY_COUNTRY[country]
    bban_length = total_length - 4
    # numpy's int64 caps at 10**19; the longest BBAN is 29 digits
    # (RU). Build the BBAN as a string by drawing one digit at a time.
    bban = "".join(str(int(rng.integers(0, 10))) for _ in range(bban_length))
    return _generate_iban_from_country_and_bban(country, bban)


_IBAN_REGEX = r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$"


class IbanAdapter:
    backend_type: str = "decoy_native"
    backend_version: str = "iban/v1"

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        if provider != "synthetic_iban":
            raise ProviderError(code="unknown_provider", message=f"got {provider!r}")
        if spec.deterministic and source_value is not None:
            canonical = (
                source_value
                if isinstance(source_value, bytes)
                else _canonicalize_source(source_value)
            )
            if spec.seed is None:
                raise ProviderError(
                    code="missing_seed",
                    message="IbanAdapter: deterministic mode requires spec.seed.",
                )
            if spec.namespace is None:
                raise ProviderError(
                    code="missing_namespace",
                    message="IbanAdapter: deterministic mode requires spec.namespace.",
                )
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=IbanDomain(rng_config=spec.extra),
            )
        rng = np.random.default_rng()
        return generate_random(rng=rng, locale=spec.locale or "en_US")

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        if spec.deterministic:
            raise ProviderError(
                code="batch_deterministic_unsupported",
                message="IbanAdapter.generate_batch does not support deterministic mode.",
            )
        rng = np.random.default_rng()
        return [generate_random(rng=rng, locale=spec.locale or "en_US") for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return CapabilityMatrix(
            provider="synthetic_iban",
            backend_type=self.backend_type,
            backend_version=self.backend_version,
            supports_deterministic=True,
            supports_uniqueness=True,
            supports_value_reuse=True,
            preserves_source_cardinality=True,
            participates_in_fk_pk=True,
            poolable=False,
            supported_locales=("en_US",),
            supports_coherent_link=False,
            format_regex=_IBAN_REGEX,
            blocklist_validators=("iban_mod97_check",),
            fallback_behavior="fail_plan_compile",
        )

"""NPI identifier: Domain + Adapter + Validator + generate_random.

Format: 10 digits, last is Luhn check digit per the NPPES spec.

NPPES (National Plan and Provider Enumeration System) source:
https://www.cms.gov/Regulations-and-Guidance/Administrative-Simplification/NationalProvIdentStand/Downloads/NPIcheckdigit.pdf

Validation:
- First digit must be 1 or 2 (NPPES allocation rule).
- Mod-10 Luhn check with prefix `80840` per CMS NPI check-digit spec.

Quarterly source review: NPPES is stable (last major update: 2008).
Last reviewed: 2026-05-27.
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

_NPI_LUHN_PREFIX = "80840"


def _luhn_check_digit(digits: str) -> int:
    """Compute the mod-10 Luhn check digit for `digits` (NPI uses 14-digit
    input: 5-digit prefix + 9-digit NPI body)."""
    total = 0
    # Process digits right to left; every second digit doubled.
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return (10 - (total % 10)) % 10


def _generate_npi_from_body(body9: str) -> str:
    """Body9 = 9 digits (first digit must be 1 or 2). Returns 10-digit NPI."""
    luhn_input = _NPI_LUHN_PREFIX + body9
    check = _luhn_check_digit(luhn_input)
    return body9 + str(check)


def _is_valid_npi(npi: str) -> bool:
    if len(npi) != 10 or not npi.isdigit():
        return False
    if npi[0] not in ("1", "2"):
        return False
    expected_check = _luhn_check_digit(_NPI_LUHN_PREFIX + npi[:9])
    return int(npi[9]) == expected_check


@dataclass(frozen=True)
class NpiDomain:
    rng_config: dict[str, Any] | None = None

    def from_bytes(self, b: bytes) -> str:
        if len(b) != 32:
            raise IdentifierError(
                code="invalid_input_length",
                message=f"NpiDomain.from_bytes expects 32 bytes; got {len(b)}.",
            )
        # First digit is 1 or 2 (allocate roughly half-and-half from byte[0] parity).
        first = "1" if b[0] % 2 == 0 else "2"
        # Body digits 2-9 from bytes[1:9].
        rest8 = int.from_bytes(b[1:9], "big") % 100_000_000
        body9 = first + f"{rest8:08d}"
        return _generate_npi_from_body(body9)


class NpiValidator:
    @staticmethod
    def is_valid(value: str) -> bool:
        return _is_valid_npi(value)


def generate_random(rng: np.random.Generator, locale: str = "en_US") -> str:
    if locale != "en_US":
        raise ProviderError(
            code="unsupported_locale",
            message=f"NPI is US-only; got locale={locale!r}.",
        )
    first = str(int(rng.choice([1, 2])))
    rest8 = int(rng.integers(0, 100_000_000))
    return _generate_npi_from_body(first + f"{rest8:08d}")


_NPI_REGEX = r"^[12]\d{9}$"


class NpiAdapter:
    backend_type: str = "decoy_native"
    backend_version: str = "npi/v1"

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        if provider != "synthetic_npi":
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
                    message="NpiAdapter: deterministic mode requires spec.seed.",
                )
            if spec.namespace is None:
                raise ProviderError(
                    code="missing_namespace",
                    message="NpiAdapter: deterministic mode requires spec.namespace.",
                )
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=NpiDomain(rng_config=spec.extra),
            )
        rng = np.random.default_rng()
        return generate_random(rng=rng, locale=spec.locale or "en_US")

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        if spec.deterministic:
            raise ProviderError(
                code="batch_deterministic_unsupported",
                message="NpiAdapter.generate_batch does not support deterministic mode.",
            )
        rng = np.random.default_rng()
        return [generate_random(rng=rng, locale=spec.locale or "en_US") for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return CapabilityMatrix(
            provider="synthetic_npi",
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
            format_regex=_NPI_REGEX,
            blocklist_validators=("nppes_luhn_check",),
            fallback_behavior="fail_plan_compile",
        )

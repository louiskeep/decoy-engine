"""PAN (Primary Account Number) identifier: 16-digit Luhn-valid number.

Format: 16 digits, last is the Luhn check digit per ISO/IEC 7812-1.
The leading 6 digits are an Issuer Identification Number (IIN); the
default issuer is the canonical Faker test prefix `4` (Visa-shaped)
so any downstream system that checks IIN ranges sees a recognizable
brand. The 9 body digits in between are derived from the seed.

ISO/IEC 7812-1 source:
https://www.iso.org/standard/70484.html

Validation:
- 16 digits.
- Mod-10 Luhn check across all 16 digits.

Quarterly source review: ISO/IEC 7812-1 unchanged since the 2017
edition. Last reviewed: 2026-06-01.

MG-1 S4 (2026-06-01).
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


# Canonical Faker test prefix (Visa-shaped, 4xxx). The first digit is
# the Major Industry Identifier (MII); 4 is the bank/financial MII.
_DEFAULT_IIN = "411111"


def _luhn_check_digit(digits: str) -> int:
    """Compute the mod-10 Luhn check digit for the digit string left of
    the check position.

    Per ISO/IEC 7812-1 the input has 15 digits; the output is the
    16th. Each digit from the right is doubled at odd positions; sums
    over 9 collapse via digit-sum (or equivalently `n - 9`).
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return (10 - (total % 10)) % 10


def _generate_pan_from_body(body9: str, iin: str = _DEFAULT_IIN) -> str:
    """Combine `iin` (6 digits) + `body9` (9 digits) + check (1 digit)."""
    luhn_input = iin + body9
    check = _luhn_check_digit(luhn_input)
    return luhn_input + str(check)


def _is_valid_pan(pan: str) -> bool:
    if len(pan) != 16 or not pan.isdigit():
        return False
    expected_check = _luhn_check_digit(pan[:15])
    return int(pan[15]) == expected_check


@dataclass(frozen=True)
class PanDomain:
    rng_config: dict[str, Any] | None = None

    def from_bytes(self, b: bytes) -> str:
        if len(b) != 32:
            raise IdentifierError(
                code="invalid_input_length",
                message=f"PanDomain.from_bytes expects 32 bytes; got {len(b)}.",
            )
        # Body9 from bytes[1:9]; mod by 10^9 to fit.
        rest9 = int.from_bytes(b[0:9], "big") % 1_000_000_000
        body9 = f"{rest9:09d}"
        return _generate_pan_from_body(body9)


class PanValidator:
    @staticmethod
    def is_valid(value: str) -> bool:
        return _is_valid_pan(value)


def generate_random(rng: np.random.Generator, locale: str = "en_US") -> str:
    rest9 = int(rng.integers(0, 1_000_000_000))
    return _generate_pan_from_body(f"{rest9:09d}")


_PAN_REGEX = r"^\d{16}$"


class PanAdapter:
    backend_type: str = "decoy_native"
    backend_version: str = "pan/v1"

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        if provider != "synthetic_pan":
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
                    message="PanAdapter: deterministic mode requires spec.seed.",
                )
            if spec.namespace is None:
                raise ProviderError(
                    code="missing_namespace",
                    message="PanAdapter: deterministic mode requires spec.namespace.",
                )
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=PanDomain(rng_config=spec.extra),
            )
        rng = np.random.default_rng()
        return generate_random(rng=rng, locale=spec.locale or "en_US")

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        if spec.deterministic:
            raise ProviderError(
                code="batch_deterministic_unsupported",
                message="PanAdapter.generate_batch does not support deterministic mode.",
            )
        rng = np.random.default_rng()
        return [generate_random(rng=rng, locale=spec.locale or "en_US") for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return CapabilityMatrix(
            provider="synthetic_pan",
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
            format_regex=_PAN_REGEX,
            blocklist_validators=("luhn_check",),
            fallback_behavior="fail_plan_compile",
        )

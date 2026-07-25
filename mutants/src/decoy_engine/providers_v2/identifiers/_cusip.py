"""CUSIP identifier: 9-character security identifier issued by CUSIP
Global Services (CGS) under license from the American Bankers
Association.

Format per CUSIP Global Services Issuer Numbering Convention:
- Position 1-6: issuer number (alphanumeric).
- Position 7-8: issue number (alphanumeric; 88-99 reserved for
  private issuance, 00-99 valid in practice).
- Position 9: modified-Luhn check digit (0-9).

Modified Luhn algorithm:
- Each character is converted to a numeric value:
  0-9   -> 0-9
  A-Z   -> 10-35  (A=10, B=11, ..., Z=35)
  '*'   -> 36
  '@'   -> 37
  '#'   -> 38
- Every odd-position character (1-based, so positions 2, 4, 6, 8) is
  doubled (the standard Luhn alternation, but indexed left-to-right
  on a fixed 8-char body instead of right-to-left on a variable
  length).
- All resulting values are summed; for values >= 10 the digit-sum is
  used (or equivalently `n // 10 + n % 10`).
- Check digit = (10 - sum % 10) % 10.

CGS source:
https://www.cusip.com/identifiers.html

Quarterly source review: CUSIP convention stable since 1968 (last
algorithm change). Last reviewed: 2026-06-01.

MG-1 S4 (2026-06-01); fourth and final new domain generator in this
sub-slice. NPI shipped in engine-v2 S6.
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

# Body alphabet: 0-9, A-Z, *, @, # per CGS spec.
_CUSIP_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*@#"


def _char_value(ch: str) -> int:
    """Convert one CUSIP character to its numeric value per CGS.

    Digits 0-9 map to themselves; A-Z map to 10-35; '*' / '@' / '#'
    map to 36 / 37 / 38. Raises for any other character.
    """
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return ord(ch) - ord("A") + 10
    if ch == "*":
        return 36
    if ch == "@":
        return 37
    if ch == "#":
        return 38
    raise IdentifierError(
        code="invalid_cusip_char",
        message=f"CUSIP character {ch!r} not in alphabet 0-9A-Z*@#.",
    )


def _luhn_check_digit(body8: str) -> int:
    """Compute the CUSIP modified-Luhn check digit for an 8-char body.

    Per CGS: indices 0, 2, 4, 6 are 'odd-numbered position' if we
    count from 1. Standard CGS reference doubles those that are 'in
    even position when counted from the right' -- because the body
    has fixed length 8, that's equivalent to doubling positions
    0, 2, 4, 6 (zero-indexed). See the CGS reference implementation.
    """
    total = 0
    for i, ch in enumerate(body8):
        v = _char_value(ch)
        if i % 2 == 1:  # 2nd, 4th, 6th, 8th positions (0-indexed: 1, 3, 5, 7)
            v *= 2
        # Digit-sum for values >= 10. e.g. 14 -> 1+4 = 5.
        v = (v // 10) + (v % 10)
        total += v
    return (10 - (total % 10)) % 10


def _is_valid_cusip(cusip: str) -> bool:
    if len(cusip) != 9:
        return False
    body = cusip[:8]
    # Body must be drawn from the CUSIP alphabet.
    for ch in body:
        if ch not in _CUSIP_ALPHABET:
            return False
    # Check digit must be 0-9.
    if not cusip[8].isdigit():
        return False
    expected_check = _luhn_check_digit(body)
    return int(cusip[8]) == expected_check


def _generate_cusip_from_body(body8: str) -> str:
    """Body8 = 8 chars from the CUSIP alphabet. Returns 9-char CUSIP."""
    check = _luhn_check_digit(body8)
    return body8 + str(check)


def _body_from_int(n: int) -> str:
    """Convert an int to an 8-char body using the CUSIP alphabet
    (base 39). Pads with leading zeros."""
    base = len(_CUSIP_ALPHABET)  # 39
    body = ""
    for _ in range(8):
        body = _CUSIP_ALPHABET[n % base] + body
        n //= base
    return body


@dataclass(frozen=True)
class CusipDomain:
    rng_config: dict[str, Any] | None = None

    def from_bytes(self, b: bytes) -> str:
        if len(b) != 32:
            raise IdentifierError(
                code="invalid_input_length",
                message=f"CusipDomain.from_bytes expects 32 bytes; got {len(b)}.",
            )
        # 39**8 ~ 5.3e12 fits in int64; use the first 8 bytes.
        n = int.from_bytes(b[0:8], "big") % (len(_CUSIP_ALPHABET) ** 8)
        body = _body_from_int(n)
        return _generate_cusip_from_body(body)


class CusipValidator:
    @staticmethod
    def is_valid(value: str) -> bool:
        return _is_valid_cusip(value)


def generate_random(rng: np.random.Generator, locale: str = "en_US") -> str:
    base = len(_CUSIP_ALPHABET)
    # 39**8 = 5.3e12; well within int64. Sample one char at a time
    # for clarity (Python int math anyway).
    body = "".join(_CUSIP_ALPHABET[int(rng.integers(0, base))] for _ in range(8))
    return _generate_cusip_from_body(body)


_CUSIP_REGEX = r"^[0-9A-Z*@#]{8}\d$"


class CusipAdapter:
    backend_type: str = "decoy_native"
    backend_version: str = "cusip/v1"

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        if provider != "synthetic_cusip":
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
                    message="CusipAdapter: deterministic mode requires spec.seed.",
                )
            if spec.namespace is None:
                raise ProviderError(
                    code="missing_namespace",
                    message="CusipAdapter: deterministic mode requires spec.namespace.",
                )
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=CusipDomain(rng_config=spec.extra),
            )
        rng = np.random.default_rng()
        return generate_random(rng=rng, locale=spec.locale or "en_US")

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        if spec.deterministic:
            raise ProviderError(
                code="batch_deterministic_unsupported",
                message="CusipAdapter.generate_batch does not support deterministic mode.",
            )
        rng = np.random.default_rng()
        return [generate_random(rng=rng, locale=spec.locale or "en_US") for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return CapabilityMatrix(
            provider="synthetic_cusip",
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
            format_regex=_CUSIP_REGEX,
            blocklist_validators=("cusip_modified_luhn",),
            fallback_behavior="fail_plan_compile",
        )

"""EIN identifier: Domain + Adapter + Validator + generate_random.

Format: `XX-XXXXXXX` 9 digits with hyphen after first 2.

IRS Form SS-4 + Publication 15 source:
https://www.irs.gov/businesses/small-businesses-self-employed/how-eins-are-assigned-and-valid-ein-prefixes

Valid EIN prefixes (first two digits) drawn from IRS published list
(~80 prefixes covering Andover, Atlanta, Austin, Brookhaven, Cincinnati,
Fresno, Internet, Kansas City, Memphis, Ogden, Philadelphia, Small
Business Online, SBSE).

Quarterly source review: IRS prefix list grows annually.
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

# IRS published valid EIN prefixes (subset; representative).
_VALID_PREFIXES: tuple[int, ...] = (
    1,
    2,
    3,
    4,
    5,
    6,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    20,
    21,
    22,
    23,
    24,
    25,
    26,
    27,
    30,
    31,
    32,
    33,
    34,
    35,
    36,
    37,
    38,
    39,
    40,
    41,
    42,
    43,
    44,
    45,
    46,
    47,
    48,
    50,
    51,
    52,
    53,
    54,
    55,
    56,
    57,
    58,
    59,
    60,
    61,
    62,
    63,
    64,
    65,
    66,
    67,
    68,
    71,
    72,
    73,
    74,
    75,
    76,
    77,
    80,
    81,
    82,
    83,
    84,
    85,
    86,
    87,
    88,
    90,
    91,
    92,
    93,
    94,
    95,
    98,
    99,
)
_VALID_PREFIX_SET: frozenset[int] = frozenset(_VALID_PREFIXES)


def _is_valid_ein(ein_digits: str) -> bool:
    if len(ein_digits) != 9 or not ein_digits.isdigit():
        return False
    return int(ein_digits[:2]) in _VALID_PREFIX_SET


def _format_ein_display(ein_digits: str) -> str:
    return f"{ein_digits[:2]}-{ein_digits[2:]}"


@dataclass(frozen=True)
class EinDomain:
    rng_config: dict[str, Any] | None = None

    def from_bytes(self, b: bytes) -> str:
        if len(b) != 32:
            raise IdentifierError(
                code="invalid_input_length",
                message=f"EinDomain.from_bytes expects 32 bytes; got {len(b)}.",
            )
        # Pick a prefix deterministically from bytes[0:4]; pick the last 7
        # digits from bytes[4:12].
        prefix_idx = int.from_bytes(b[0:4], "big") % len(_VALID_PREFIXES)
        prefix = _VALID_PREFIXES[prefix_idx]
        last7 = int.from_bytes(b[4:12], "big") % 10_000_000
        return f"{prefix:02d}-{last7:07d}"


class EinValidator:
    @staticmethod
    def is_valid(value: str) -> bool:
        parts = value.split("-")
        if len(parts) != 2 or [len(p) for p in parts] != [2, 7]:
            return False
        return _is_valid_ein("".join(parts))


def generate_random(rng: np.random.Generator, locale: str = "en_US") -> str:
    if locale != "en_US":
        raise ProviderError(
            code="unsupported_locale",
            message=f"EIN is US-only; got locale={locale!r}.",
        )
    prefix = int(rng.choice(_VALID_PREFIXES))
    last7 = int(rng.integers(0, 10_000_000))
    return f"{prefix:02d}-{last7:07d}"


_EIN_REGEX = r"^\d{2}-\d{7}$"


class EinAdapter:
    backend_type: str = "decoy_native"
    backend_version: str = "ein/v1"

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        if provider != "synthetic_ein":
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
                    message="EinAdapter: deterministic mode requires spec.seed.",
                )
            if spec.namespace is None:
                raise ProviderError(
                    code="missing_namespace",
                    message="EinAdapter: deterministic mode requires spec.namespace.",
                )
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=EinDomain(rng_config=spec.extra),
            )
        rng = np.random.default_rng()
        return generate_random(rng=rng, locale=spec.locale or "en_US")

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        if spec.deterministic:
            raise ProviderError(
                code="batch_deterministic_unsupported",
                message="EinAdapter.generate_batch does not support deterministic mode.",
            )
        rng = np.random.default_rng()
        return [generate_random(rng=rng, locale=spec.locale or "en_US") for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return CapabilityMatrix(
            provider="synthetic_ein",
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
            format_regex=_EIN_REGEX,
            blocklist_validators=("irs_prefix_list",),
            fallback_behavior="fail_plan_compile",
        )

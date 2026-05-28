"""SSN identifier: Domain + Adapter + Validator + generate_random.

Format: 9 digits formatted as `XXX-XX-XXXX`.

SSA POMS GN 02601.090 ("Invalid SSN ranges") source:
https://secure.ssa.gov/poms.nsf/lnx/0202601090

Blocklist:
- Area (first 3 digits) must not be 000, 666, or 900-999.
- Group (digits 4-5) must not be 00.
- Serial (digits 6-9) must not be 0000.

Per S6 spec §5 + §3.1: deterministic mode routes through derive_value
with SsnDomain; non-deterministic through generate_random. PoolAdapter
does NOT wrap (poolable=False per CapabilityMatrix).

Quarterly source review: SSA POMS rev tracked at the URL above.
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

# SSA POMS blocklist:
_INVALID_AREAS: frozenset[int] = frozenset({0, 666, *range(900, 1000)})


def _is_blocklisted(ssn_digits: str) -> bool:
    """Check whether a 9-digit SSN string violates SSA POMS rules."""
    if len(ssn_digits) != 9 or not ssn_digits.isdigit():
        return True
    area = int(ssn_digits[:3])
    group = int(ssn_digits[3:5])
    serial = int(ssn_digits[5:])
    if area in _INVALID_AREAS:
        return True
    if group == 0:
        return True
    return serial == 0


def _format_ssn(value: int) -> str:
    """Turn a uint into a 9-digit zero-padded SSN string `XXXXXXXXX`."""
    nine_digits = value % 1_000_000_000
    return f"{nine_digits:09d}"


def _format_ssn_display(ssn_digits: str) -> str:
    """Format 9 raw digits as `XXX-XX-XXXX`."""
    return f"{ssn_digits[:3]}-{ssn_digits[3:5]}-{ssn_digits[5:]}"


@dataclass(frozen=True)
class SsnDomain:
    """Pure mapping from 32 derive() bytes to a valid SSN string.

    Per S6 spec §5 + S3 spec §3.3 Domain protocol contract:
    `from_bytes(b)` must be pure (no I/O, no process state). Walks the
    32 bytes in 4 x 8-byte slices; returns the first non-blocklisted
    SSN. All 4 slices failing raises IdentifierError(blocklist_exhausted).
    """

    rng_config: dict[str, Any] | None = None

    def from_bytes(self, b: bytes) -> str:
        if len(b) != 32:
            raise IdentifierError(
                code="invalid_input_length",
                message=f"SsnDomain.from_bytes expects 32 bytes; got {len(b)}.",
            )
        for offset in (0, 8, 16, 24):
            value = int.from_bytes(b[offset : offset + 8], "big")
            candidate = _format_ssn(value)
            if not _is_blocklisted(candidate):
                return _format_ssn_display(candidate)
        raise IdentifierError(
            code="blocklist_exhausted",
            message=(
                "All 4 derive() rehash offsets produced SSA POMS-blocklisted "
                "SSNs. Practically unreachable (~1.5e-4 probability); suggests "
                "fixture input or a bug in the blocklist."
            ),
        )


class SsnValidator:
    """Validate an SSN string against SSA POMS format + blocklist."""

    @staticmethod
    def is_valid(value: str) -> bool:
        parts = value.split("-")
        if len(parts) != 3 or [len(p) for p in parts] != [3, 2, 4]:
            return False
        digits = "".join(parts)
        return not _is_blocklisted(digits)


def generate_random(rng: np.random.Generator, locale: str = "en_US") -> str:
    """Non-deterministic SSN generator. Loops until a non-blocklisted draw."""
    if locale != "en_US":
        raise ProviderError(
            code="unsupported_locale",
            message=f"SSN is US-only; got locale={locale!r}.",
        )
    while True:
        value = int(rng.integers(0, 1_000_000_000))
        candidate = _format_ssn(value)
        if not _is_blocklisted(candidate):
            return _format_ssn_display(candidate)


_SSN_REGEX = r"^\d{3}-\d{2}-\d{4}$"


class SsnAdapter:
    """Concrete BackendAdapter for `synthetic_ssn` (S6 swap target)."""

    backend_type: str = "decoy_native"
    backend_version: str = "ssn/v1"

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        if provider != "synthetic_ssn":
            raise ProviderError(
                code="unknown_provider",
                message=f"SsnAdapter only handles synthetic_ssn; got {provider!r}.",
            )
        if spec.deterministic and source_value is not None:
            if isinstance(source_value, bytes):
                canonical = source_value
            else:
                canonical = _canonicalize_source(source_value)
            assert spec.seed is not None  # noqa: S101 -- enforced by ProviderSpec __post_init__
            assert spec.namespace is not None  # noqa: S101
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=SsnDomain(rng_config=spec.extra),
            )
        rng = np.random.default_rng()
        return generate_random(rng=rng, locale=spec.locale or "en_US")

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        if spec.deterministic:
            raise ProviderError(
                code="batch_deterministic_unsupported",
                message=(
                    "SsnAdapter.generate_batch does not support deterministic mode; "
                    "deterministic callers use generate(...) per row. PoolAdapter "
                    "cannot wrap SsnAdapter (poolable=False per §3.1)."
                ),
            )
        rng = np.random.default_rng()
        return [generate_random(rng=rng, locale=spec.locale or "en_US") for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return CapabilityMatrix(
            provider="synthetic_ssn",
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
            format_regex=_SSN_REGEX,
            blocklist_validators=("ssa_pom_blocklist",),
            fallback_behavior="fail_plan_compile",
        )

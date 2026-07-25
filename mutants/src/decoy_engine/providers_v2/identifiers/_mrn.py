"""MRN identifier: Domain + Adapter + Validator + generate_random.

Format: configurable digits + optional alpha prefix. The engine ships
a conservative default (8 digits, no leading zero); customers override
via spec.extra:

- `mrn_digit_count`: int (default 8)
- `mrn_alpha_prefix`: str (default ""; e.g. "MRN" -> "MRN12345678")
- `allow_leading_zero`: bool (default False)

MRN format varies per site so the spec is engine-default + customer
override.

Quarterly source review: no canonical source (per-site).
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

_DEFAULT_DIGITS = 8


def _is_valid_mrn(
    value: str, digit_count: int, alpha_prefix: str, allow_leading_zero: bool
) -> bool:
    if not value.startswith(alpha_prefix):
        return False
    digit_part = value[len(alpha_prefix) :]
    if len(digit_part) != digit_count or not digit_part.isdigit():
        return False
    return not (not allow_leading_zero and digit_part[0] == "0")


def _format_mrn(
    value_int: int, digit_count: int, alpha_prefix: str, allow_leading_zero: bool
) -> str:
    digits = f"{value_int % (10**digit_count):0{digit_count}d}"
    if not allow_leading_zero and digits[0] == "0":
        # Bump leading digit deterministically rather than rejecting.
        digits = "1" + digits[1:]
    return alpha_prefix + digits


@dataclass(frozen=True)
class MrnDomain:
    rng_config: dict[str, Any] | None = None

    def from_bytes(self, b: bytes) -> str:
        if len(b) != 32:
            raise IdentifierError(
                code="invalid_input_length",
                message=f"MrnDomain.from_bytes expects 32 bytes; got {len(b)}.",
            )
        cfg = self.rng_config or {}
        digit_count = int(cfg.get("mrn_digit_count", _DEFAULT_DIGITS))
        alpha_prefix = str(cfg.get("mrn_alpha_prefix", ""))
        allow_leading_zero = bool(cfg.get("allow_leading_zero", False))
        value = int.from_bytes(b[0:8], "big")
        return _format_mrn(value, digit_count, alpha_prefix, allow_leading_zero)


class MrnValidator:
    @staticmethod
    def is_valid(
        value: str,
        *,
        digit_count: int = _DEFAULT_DIGITS,
        alpha_prefix: str = "",
        allow_leading_zero: bool = False,
    ) -> bool:
        return _is_valid_mrn(value, digit_count, alpha_prefix, allow_leading_zero)


def generate_random(
    rng: np.random.Generator,
    locale: str = "en_US",
    *,
    digit_count: int = _DEFAULT_DIGITS,
    alpha_prefix: str = "",
    allow_leading_zero: bool = False,
) -> str:
    # MRN is locale-agnostic; ignore locale gate (no SSA-style restriction).
    value = int(rng.integers(0, 10**digit_count))
    return _format_mrn(value, digit_count, alpha_prefix, allow_leading_zero)


_MRN_REGEX = r"^[A-Za-z]*\d+$"


class MrnAdapter:
    backend_type: str = "decoy_native"
    backend_version: str = "mrn/v1"

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        if provider != "synthetic_mrn":
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
                    message="MrnAdapter: deterministic mode requires spec.seed.",
                )
            if spec.namespace is None:
                raise ProviderError(
                    code="missing_namespace",
                    message="MrnAdapter: deterministic mode requires spec.namespace.",
                )
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=MrnDomain(rng_config=spec.extra),
            )
        rng = np.random.default_rng()
        cfg = spec.extra or {}
        return generate_random(
            rng=rng,
            locale=spec.locale or "en_US",
            digit_count=int(cfg.get("mrn_digit_count", _DEFAULT_DIGITS)),
            alpha_prefix=str(cfg.get("mrn_alpha_prefix", "")),
            allow_leading_zero=bool(cfg.get("allow_leading_zero", False)),
        )

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        if spec.deterministic:
            raise ProviderError(
                code="batch_deterministic_unsupported",
                message="MrnAdapter.generate_batch does not support deterministic mode.",
            )
        rng = np.random.default_rng()
        cfg = spec.extra or {}
        return [
            generate_random(
                rng=rng,
                locale=spec.locale or "en_US",
                digit_count=int(cfg.get("mrn_digit_count", _DEFAULT_DIGITS)),
                alpha_prefix=str(cfg.get("mrn_alpha_prefix", "")),
                allow_leading_zero=bool(cfg.get("allow_leading_zero", False)),
            )
            for _ in range(count)
        ]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return CapabilityMatrix(
            provider="synthetic_mrn",
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
            format_regex=_MRN_REGEX,
            blocklist_validators=("mrn_format",),
            fallback_behavior="fail_plan_compile",
        )

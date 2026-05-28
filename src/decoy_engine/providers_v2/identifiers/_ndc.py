"""NDC identifier: Domain + Adapter + Validator + generate_random.

Format: `NNNNN-NNNN-NN` segments (three valid segment-length variants).

FDA NDC Directory source:
https://www.accessdata.fda.gov/scripts/cder/ndc/index.cfm

Three segment-length variants per the FDA Drug Labeler Code spec:
- `4-4-2` (4-digit labeler + 4-digit product + 2-digit package)
- `5-3-2` (5-digit labeler + 3-digit product + 2-digit package)
- `5-4-1` (5-digit labeler + 4-digit product + 1-digit package)

Default variant: `5-4-2` (the FDA-published 11-digit billing format,
HIPAA-standard). Spec.extra can override via `ndc_segment_layout`.

Quarterly source review: FDA NDC variants stable.
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

# Default segment layout: (labeler_digits, product_digits, package_digits).
_DEFAULT_LAYOUT: tuple[int, int, int] = (5, 4, 2)

_VALID_LAYOUTS: frozenset[tuple[int, int, int]] = frozenset(
    {
        (4, 4, 2),
        (5, 3, 2),
        (5, 4, 1),
        (5, 4, 2),  # 11-digit billing format
    }
)


def _format_ndc(layout: tuple[int, int, int], labeler: int, product: int, package: int) -> str:
    labeler_d, product_d, package_d = layout
    return f"{labeler:0{labeler_d}d}-{product:0{product_d}d}-{package:0{package_d}d}"


def _is_valid_ndc(value: str) -> bool:
    parts = value.split("-")
    if len(parts) != 3:
        return False
    lens = tuple(len(p) for p in parts)
    if lens not in _VALID_LAYOUTS:
        return False
    return all(p.isdigit() for p in parts)


@dataclass(frozen=True)
class NdcDomain:
    rng_config: dict[str, Any] | None = None

    def from_bytes(self, b: bytes) -> str:
        if len(b) != 32:
            raise IdentifierError(
                code="invalid_input_length",
                message=f"NdcDomain.from_bytes expects 32 bytes; got {len(b)}.",
            )
        layout: tuple[int, int, int] = _DEFAULT_LAYOUT
        if self.rng_config and "ndc_segment_layout" in self.rng_config:
            raw_layout = self.rng_config["ndc_segment_layout"]
            if isinstance(raw_layout, list | tuple) and len(raw_layout) == 3:
                candidate = tuple(int(x) for x in raw_layout)
                if candidate in _VALID_LAYOUTS:
                    # Reconstruct as 3-tuple literal so mypy infers tuple[int, int, int].
                    layout = (candidate[0], candidate[1], candidate[2])
        labeler_d, product_d, package_d = layout
        labeler = int.from_bytes(b[0:4], "big") % (10**labeler_d)
        product = int.from_bytes(b[4:8], "big") % (10**product_d)
        package = int.from_bytes(b[8:12], "big") % (10**package_d)
        return _format_ndc(layout, labeler, product, package)


class NdcValidator:
    @staticmethod
    def is_valid(value: str) -> bool:
        return _is_valid_ndc(value)


def generate_random(rng: np.random.Generator, locale: str = "en_US") -> str:
    if locale != "en_US":
        raise ProviderError(
            code="unsupported_locale",
            message=f"NDC is US-only; got locale={locale!r}.",
        )
    layout = _DEFAULT_LAYOUT
    labeler_d, product_d, package_d = layout
    labeler = int(rng.integers(0, 10**labeler_d))
    product = int(rng.integers(0, 10**product_d))
    package = int(rng.integers(0, 10**package_d))
    return _format_ndc(layout, labeler, product, package)


_NDC_REGEX = r"^\d{4,5}-\d{3,4}-\d{1,2}$"


class NdcAdapter:
    backend_type: str = "decoy_native"
    backend_version: str = "ndc/v1"

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        if provider != "synthetic_ndc":
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
                    message="NdcAdapter: deterministic mode requires spec.seed.",
                )
            if spec.namespace is None:
                raise ProviderError(
                    code="missing_namespace",
                    message="NdcAdapter: deterministic mode requires spec.namespace.",
                )
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=NdcDomain(rng_config=spec.extra),
            )
        rng = np.random.default_rng()
        return generate_random(rng=rng, locale=spec.locale or "en_US")

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        if spec.deterministic:
            raise ProviderError(
                code="batch_deterministic_unsupported",
                message="NdcAdapter.generate_batch does not support deterministic mode.",
            )
        rng = np.random.default_rng()
        return [generate_random(rng=rng, locale=spec.locale or "en_US") for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return CapabilityMatrix(
            provider="synthetic_ndc",
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
            format_regex=_NDC_REGEX,
            blocklist_validators=("fda_ndc_segment_format",),
            fallback_behavior="fail_plan_compile",
        )

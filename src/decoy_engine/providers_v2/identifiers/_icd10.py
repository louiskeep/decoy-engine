"""ICD-10-CM identifier: clinical diagnosis code.

Format per CDC ICD-10-CM Official Guidelines:
- Position 1: letter (chapter; A-Z).
- Positions 2-3: 2 digits (category).
- Position 4 (after `.`): 1 digit (etiology / anatomic site).
- Positions 5-7: 0-3 optional digits or letters (subcategory).

We emit codes in the 4-5 character "category.subcategory" shape
(e.g. "J18.9" -- pneumonia, unspecified) because that's the
canonical operational granularity for downstream EHR + claims
systems. Longer codes (7-character episode-of-care extensions) are
out of scope for the synthetic generator -- they require domain
context that this lightweight provider does not carry.

Validation (mirrors decoy_engine.storm.detectors._icd10_valid):
- 3-7 alphanumeric chars (dots stripped).
- Position 0 is a letter belonging to a real ICD-10 chapter.
- Positions 1-2 (category) fall within the chapter's valid range.

CDC source:
https://www.cdc.gov/nchs/icd/icd-10-cm.htm

Quarterly source review: ICD-10-CM is updated annually (October
release). The chapter table is conservative + accommodates the
2026 release. Last reviewed: 2026-06-01.

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
from decoy_engine.storm.detectors import _ICD10_CHAPTERS, _icd10_valid


# Chapter letters as a stable list so deterministic byte->code maps
# pick the same chapter for the same input every time. Order matches
# A-Z alphabetic.
_CHAPTERS_ORDERED: tuple[str, ...] = tuple(sorted(_ICD10_CHAPTERS.keys()))


def _generate_icd10_from_bytes(b: bytes) -> str:
    """Map 32 random bytes onto a valid ICD-10-CM 4-character code:

    - chapter letter from b[0] mod chapter_count
    - category from b[1:3] mod (chapter_hi - chapter_lo + 1) + chapter_lo
    - subcategory digit from b[3] mod 10
    """
    chapter_idx = b[0] % len(_CHAPTERS_ORDERED)
    chapter = _CHAPTERS_ORDERED[chapter_idx]
    cat_lo, cat_hi = _ICD10_CHAPTERS[chapter]
    cat_offset = int.from_bytes(b[1:3], "big") % (cat_hi - cat_lo + 1)
    category = cat_lo + cat_offset
    subcategory = b[3] % 10
    return f"{chapter}{category:02d}.{subcategory}"


@dataclass(frozen=True)
class Icd10Domain:
    rng_config: dict[str, Any] | None = None

    def from_bytes(self, b: bytes) -> str:
        if len(b) != 32:
            raise IdentifierError(
                code="invalid_input_length",
                message=f"Icd10Domain.from_bytes expects 32 bytes; got {len(b)}.",
            )
        return _generate_icd10_from_bytes(b)


class Icd10Validator:
    @staticmethod
    def is_valid(value: str) -> bool:
        return _icd10_valid(value)


def generate_random(rng: np.random.Generator, locale: str = "en_US") -> str:
    chapter = _CHAPTERS_ORDERED[rng.integers(0, len(_CHAPTERS_ORDERED))]
    cat_lo, cat_hi = _ICD10_CHAPTERS[chapter]
    category = int(rng.integers(cat_lo, cat_hi + 1))
    subcategory = int(rng.integers(0, 10))
    return f"{chapter}{category:02d}.{subcategory}"


_ICD10_REGEX = r"^[A-Z]\d{2}\.\d$"


class Icd10Adapter:
    backend_type: str = "decoy_native"
    backend_version: str = "icd10/v1"

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        if provider != "synthetic_icd10":
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
                    message="Icd10Adapter: deterministic mode requires spec.seed.",
                )
            if spec.namespace is None:
                raise ProviderError(
                    code="missing_namespace",
                    message="Icd10Adapter: deterministic mode requires spec.namespace.",
                )
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=Icd10Domain(rng_config=spec.extra),
            )
        rng = np.random.default_rng()
        return generate_random(rng=rng, locale=spec.locale or "en_US")

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        if spec.deterministic:
            raise ProviderError(
                code="batch_deterministic_unsupported",
                message="Icd10Adapter.generate_batch does not support deterministic mode.",
            )
        rng = np.random.default_rng()
        return [generate_random(rng=rng, locale=spec.locale or "en_US") for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return CapabilityMatrix(
            provider="synthetic_icd10",
            backend_type=self.backend_type,
            backend_version=self.backend_version,
            supports_deterministic=True,
            supports_uniqueness=False,
            supports_value_reuse=True,
            preserves_source_cardinality=False,
            participates_in_fk_pk=True,
            poolable=False,
            supported_locales=("en_US",),
            supports_coherent_link=False,
            format_regex=_ICD10_REGEX,
            blocklist_validators=("icd10_chapter_range",),
            fallback_behavior="fail_plan_compile",
        )

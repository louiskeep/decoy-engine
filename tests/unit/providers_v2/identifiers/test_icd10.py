"""MG-1 S4 (2026-06-01): ICD-10-CM domain generator regression cells.

Pins chapter-range correctness + deterministic generation + adapter
contracts. The chapter table is shared with storm/detectors via
_ICD10_CHAPTERS so the generator + the detector agree on what's
valid.
"""

from __future__ import annotations

import numpy as np
import pytest

from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2.identifiers._icd10 import (
    _CHAPTERS_ORDERED,
    Icd10Adapter,
    Icd10Domain,
    Icd10Validator,
    generate_random,
)
from decoy_engine.storm.detectors import _icd10_valid

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


# ── Validator ─────────────────────────────────────────────────────


class TestIcd10Validator:
    def test_canonical_codes_validate(self):
        # Real-world examples from common clinical use:
        assert Icd10Validator.is_valid("J18.9")  # pneumonia, unspecified
        assert Icd10Validator.is_valid("I10")  # essential hypertension
        assert Icd10Validator.is_valid("E11.9")  # type 2 diabetes, no complications
        assert Icd10Validator.is_valid("Z00.0")  # routine adult exam

    def test_rejects_invalid_chapter(self):
        # No ICD-10 chapter starts with a digit.
        assert not Icd10Validator.is_valid("100.0")

    def test_accepts_lowercase_uppercased(self):
        # The validator normalizes case (uppercase strip), so a
        # lowercase letter is treated as its uppercase equivalent.
        assert Icd10Validator.is_valid("j18.9")

    def test_rejects_category_outside_chapter_range(self):
        # Chapter P caps at 96; P97 doesn't exist.
        assert not Icd10Validator.is_valid("P97.0")

    def test_rejects_wrong_length(self):
        assert not Icd10Validator.is_valid("")
        assert not Icd10Validator.is_valid("A0")  # 2 chars


# ── Domain (from_bytes) ───────────────────────────────────────────


class TestIcd10Domain:
    def test_from_bytes_returns_valid_code_across_seed_space(self):
        domain = Icd10Domain()
        for seed_byte in range(0, 256, 13):
            b = bytes([seed_byte] * 32)
            code = domain.from_bytes(b)
            assert _icd10_valid(code), (
                f"Icd10Domain.from_bytes returned non-valid {code!r} for byte seed {seed_byte!r}."
            )

    def test_from_bytes_deterministic(self):
        domain = Icd10Domain()
        b = bytes([42] * 32)
        assert domain.from_bytes(b) == domain.from_bytes(b)

    def test_from_bytes_wrong_length_raises(self):
        domain = Icd10Domain()
        from decoy_engine.providers_v2.identifiers._errors import IdentifierError

        with pytest.raises(IdentifierError):
            domain.from_bytes(b"x" * 16)

    def test_from_bytes_emits_dot_separator(self):
        """Per ICD-10-CM canonical shape: 3 chars + '.' + 1 digit."""
        domain = Icd10Domain()
        b = bytes([100] * 32)
        code = domain.from_bytes(b)
        assert code[3] == "."
        assert len(code) == 5


# ── Random generator ──────────────────────────────────────────────


class TestRandomGenerator:
    def test_random_codes_are_valid(self):
        rng = np.random.default_rng(seed=42)
        for _ in range(100):
            code = generate_random(rng)
            assert _icd10_valid(code), f"Got invalid {code!r}"

    def test_random_uses_every_chapter_eventually(self):
        """Sanity check: the generator should reach every chapter
        across a large sample. Catches a regression where the chapter
        index modulo collapsed to a subset."""
        rng = np.random.default_rng(seed=7)
        chapters_seen = set()
        for _ in range(2000):
            chapters_seen.add(generate_random(rng)[0])
        assert chapters_seen == set(_CHAPTERS_ORDERED)


# ── Adapter ───────────────────────────────────────────────────────


class TestIcd10Adapter:
    def test_unknown_provider_rejected(self):
        adapter = Icd10Adapter()
        with pytest.raises(ProviderError, match="unknown_provider"):
            adapter.generate("not_icd10", spec=_spec())

    def test_random_generate(self):
        adapter = Icd10Adapter()
        spec = _spec()
        for _ in range(10):
            code = adapter.generate("synthetic_icd10", spec=spec)
            assert _icd10_valid(code)

    def test_deterministic_same_input_same_output(self):
        adapter = Icd10Adapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        out1 = adapter.generate("synthetic_icd10", spec=spec, source_value="pat-001")
        out2 = adapter.generate("synthetic_icd10", spec=spec, source_value="pat-001")
        assert out1 == out2
        assert _icd10_valid(out1)

    def test_deterministic_different_input_different_output(self):
        adapter = Icd10Adapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        out1 = adapter.generate("synthetic_icd10", spec=spec, source_value="pat-001")
        out2 = adapter.generate("synthetic_icd10", spec=spec, source_value="pat-002")
        assert out1 != out2

    def test_generate_batch(self):
        adapter = Icd10Adapter()
        spec = _spec()
        batch = adapter.generate_batch("synthetic_icd10", spec=spec, count=12)
        assert len(batch) == 12
        for code in batch:
            assert _icd10_valid(code)

    def test_batch_deterministic_unsupported(self):
        adapter = Icd10Adapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        with pytest.raises(ProviderError, match="batch_deterministic_unsupported"):
            adapter.generate_batch("synthetic_icd10", spec=spec, count=4)

    def test_capability_matrix(self):
        adapter = Icd10Adapter()
        cap = adapter.capability_matrix("synthetic_icd10")
        assert cap.provider == "synthetic_icd10"
        assert cap.supports_deterministic is True
        assert "icd10_chapter_range" in cap.blocklist_validators

"""MG-1 S4 (2026-06-01): PAN identifier provider regression cells.

Pins the Luhn check digit + deterministic generation + the
adapter's deterministic / random / batch contracts.
"""

from __future__ import annotations

import numpy as np
import pytest

from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2.identifiers._pan import (
    PanAdapter,
    PanDomain,
    PanValidator,
    _is_valid_pan,
    _luhn_check_digit,
    generate_random,
)

_SEED = (0x0123456789).to_bytes(8, "big")


def _spec(
    *, deterministic: bool = False, namespace: str | None = None, seed: bytes | None = None
) -> ProviderSpec:
    """Shorthand for the test specs. Deterministic mode requires
    namespace + seed per ProviderSpec.__post_init__."""
    return ProviderSpec(
        locale="en_US",
        deterministic=deterministic,
        namespace=namespace,
        seed=seed,
    )


# ── Luhn validator ─────────────────────────────────────────────────


class TestLuhnValidator:
    def test_canonical_test_pans_are_valid(self):
        """Industry-canonical test PANs that every payment-processing
        Luhn validator agrees on."""
        assert PanValidator.is_valid("4111111111111111")  # Visa test
        assert PanValidator.is_valid("4242424242424242")  # Stripe test
        assert PanValidator.is_valid("5555555555554444")  # Mastercard test

    def test_rejects_wrong_length(self):
        assert not PanValidator.is_valid("411111111111111")  # 15 digits
        assert not PanValidator.is_valid("41111111111111112")  # 17 digits
        assert not PanValidator.is_valid("")

    def test_rejects_non_digit(self):
        assert not PanValidator.is_valid("4111111111111A11")
        assert not PanValidator.is_valid("4111-1111-1111-1111")  # dashes

    def test_rejects_invalid_check_digit(self):
        # Last digit flipped.
        assert not PanValidator.is_valid("4111111111111112")
        assert not PanValidator.is_valid("4111111111111110")

    def test_luhn_zero_check_for_repeated_zeros(self):
        # 15 zeros + check digit. Mod-10 sum is 0; check digit is 0.
        assert _luhn_check_digit("0" * 15) == 0


# ── Domain (from_bytes) ────────────────────────────────────────────


class TestPanDomain:
    def test_from_bytes_returns_valid_pan(self):
        domain = PanDomain()
        for seed_byte in range(0, 256, 17):
            b = bytes([seed_byte] * 32)
            pan = domain.from_bytes(b)
            assert _is_valid_pan(pan), (
                f"PanDomain.from_bytes returned non-Luhn-valid {pan!r} for byte seed {seed_byte!r}."
            )

    def test_from_bytes_deterministic(self):
        """Same seed -> same PAN."""
        domain = PanDomain()
        b = bytes([42] * 32)
        assert domain.from_bytes(b) == domain.from_bytes(b)

    def test_from_bytes_wrong_length_raises(self):
        domain = PanDomain()
        from decoy_engine.providers_v2.identifiers._errors import IdentifierError

        with pytest.raises(IdentifierError):
            domain.from_bytes(b"x" * 16)

    def test_uses_default_iin_prefix(self):
        """Generated PANs start with the canonical 411111 IIN so
        downstream brand-detection sees a recognized issuer (Visa
        in this case). Stable across the seed space."""
        domain = PanDomain()
        for seed_byte in range(0, 256, 31):
            b = bytes([seed_byte] * 32)
            pan = domain.from_bytes(b)
            assert pan.startswith("411111")


# ── Random generator ──────────────────────────────────────────────


class TestRandomGenerator:
    def test_generate_random_emits_valid_pan(self):
        rng = np.random.default_rng(seed=42)
        for _ in range(50):
            pan = generate_random(rng)
            assert _is_valid_pan(pan)

    def test_generate_random_is_seedable(self):
        """Same numpy seed -> same generated sequence."""
        rng1 = np.random.default_rng(seed=7)
        rng2 = np.random.default_rng(seed=7)
        seq1 = [generate_random(rng1) for _ in range(10)]
        seq2 = [generate_random(rng2) for _ in range(10)]
        assert seq1 == seq2


# ── Adapter ───────────────────────────────────────────────────────


class TestPanAdapter:
    def test_unknown_provider_rejected(self):
        adapter = PanAdapter()
        with pytest.raises(ProviderError, match="unknown_provider"):
            adapter.generate("not_pan", spec=_spec())

    def test_random_generate_returns_valid_pan(self):
        adapter = PanAdapter()
        spec = _spec()
        for _ in range(10):
            pan = adapter.generate("synthetic_pan", spec=spec)
            assert _is_valid_pan(pan)

    def test_deterministic_same_seed_same_output(self):
        adapter = PanAdapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        out1 = adapter.generate("synthetic_pan", spec=spec, source_value="hello")
        out2 = adapter.generate("synthetic_pan", spec=spec, source_value="hello")
        assert out1 == out2
        assert _is_valid_pan(out1)

    def test_deterministic_different_input_different_output(self):
        adapter = PanAdapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        out1 = adapter.generate("synthetic_pan", spec=spec, source_value="hello")
        out2 = adapter.generate("synthetic_pan", spec=spec, source_value="world")
        assert out1 != out2

    def test_generate_batch_count(self):
        adapter = PanAdapter()
        spec = _spec()
        batch = adapter.generate_batch("synthetic_pan", spec=spec, count=8)
        assert len(batch) == 8
        for pan in batch:
            assert _is_valid_pan(pan)

    def test_batch_deterministic_unsupported(self):
        adapter = PanAdapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        with pytest.raises(ProviderError, match="batch_deterministic_unsupported"):
            adapter.generate_batch("synthetic_pan", spec=spec, count=4)

    def test_capability_matrix_shape(self):
        adapter = PanAdapter()
        cap = adapter.capability_matrix("synthetic_pan")
        assert cap.provider == "synthetic_pan"
        assert cap.backend_type == "decoy_native"
        assert cap.supports_deterministic is True
        assert "en_US" in cap.supported_locales
        assert "luhn_check" in cap.blocklist_validators

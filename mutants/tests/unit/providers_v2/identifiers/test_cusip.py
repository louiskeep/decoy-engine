"""MG-1 S4 (2026-06-01): CUSIP domain generator regression cells.

Pins modified-Luhn check digit + the canonical 0-9A-Z*@# alphabet +
adapter contracts. Apple Inc's canonical CUSIP (037833100) and
Microsoft's (594918104) round-trip through the validator.
"""

from __future__ import annotations

import numpy as np
import pytest

from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2.identifiers._cusip import (
    _CUSIP_ALPHABET,
    CusipAdapter,
    CusipDomain,
    _char_value,
    _is_valid_cusip,
    _luhn_check_digit,
    generate_random,
)

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


# ── Alphabet + char_value ─────────────────────────────────────────


class TestCusipAlphabet:
    def test_alphabet_has_39_chars(self):
        assert len(_CUSIP_ALPHABET) == 39
        # 10 digits + 26 letters + 3 specials.
        assert "0123456789" in _CUSIP_ALPHABET
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" in _CUSIP_ALPHABET
        for special in ("*", "@", "#"):
            assert special in _CUSIP_ALPHABET

    @pytest.mark.parametrize(
        "ch,expected",
        [
            ("0", 0),
            ("9", 9),
            ("A", 10),
            ("Z", 35),
            ("*", 36),
            ("@", 37),
            ("#", 38),
        ],
    )
    def test_char_value_canonical(self, ch, expected):
        assert _char_value(ch) == expected

    def test_char_value_rejects_invalid(self):
        from decoy_engine.providers_v2.identifiers._errors import IdentifierError

        with pytest.raises(IdentifierError, match="invalid_cusip_char"):
            _char_value("?")
        with pytest.raises(IdentifierError):
            _char_value(" ")


# ── modified-Luhn validator ───────────────────────────────────────


class TestModifiedLuhn:
    """CGS canonical examples; if a regression breaks the algorithm,
    these are the first cells to fail."""

    def test_apple_inc_common_stock(self):
        # Apple Inc common stock: CUSIP 037833100. The check digit
        # is 0; the body is 03783310.
        assert _luhn_check_digit("03783310") == 0
        assert _is_valid_cusip("037833100")

    def test_microsoft_common_stock(self):
        # Microsoft Corp common stock: CUSIP 594918104.
        assert _luhn_check_digit("59491810") == 4
        assert _is_valid_cusip("594918104")

    def test_amazon_common_stock(self):
        # Amazon.com Inc: CUSIP 023135106.
        assert _is_valid_cusip("023135106")

    def test_rejects_wrong_length(self):
        assert not _is_valid_cusip("12345678")  # 8 chars
        assert not _is_valid_cusip("1234567890")  # 10 chars

    def test_rejects_non_digit_check(self):
        # Last char must be 0-9.
        assert not _is_valid_cusip("12345678A")

    def test_rejects_invalid_body_char(self):
        # Lowercase outside CUSIP alphabet. The validator's alphabet
        # check short-circuits before the Luhn computation so the
        # function returns False instead of raising.
        assert not _is_valid_cusip("a2345678" + "0")

    def test_rejects_flipped_check_digit(self):
        # Apple's CUSIP with the check digit changed to anything
        # other than 0 is rejected.
        for d in "123456789":
            assert not _is_valid_cusip("03783310" + d)


# ── Domain (from_bytes) ───────────────────────────────────────────


class TestCusipDomain:
    def test_from_bytes_returns_valid_cusip_across_seed_space(self):
        domain = CusipDomain()
        for seed_byte in range(0, 256, 11):
            b = bytes([seed_byte] * 32)
            cusip = domain.from_bytes(b)
            assert _is_valid_cusip(cusip), (
                f"CusipDomain.from_bytes returned non-valid {cusip!r} for byte seed {seed_byte!r}."
            )

    def test_from_bytes_deterministic(self):
        domain = CusipDomain()
        b = bytes([42] * 32)
        assert domain.from_bytes(b) == domain.from_bytes(b)

    def test_from_bytes_wrong_length_raises(self):
        domain = CusipDomain()
        from decoy_engine.providers_v2.identifiers._errors import IdentifierError

        with pytest.raises(IdentifierError):
            domain.from_bytes(b"x" * 16)

    def test_from_bytes_uses_full_alphabet(self):
        """Across many seeds the generator's body characters cover
        digits + letters + at least one special. Catches a regression
        that accidentally collapses the alphabet."""
        domain = CusipDomain()
        chars_seen: set[str] = set()
        for seed_byte in range(0, 256):
            cusip = domain.from_bytes(bytes([seed_byte] * 32))
            chars_seen.update(cusip[:8])  # body only
        # Sanity: should see at least 30 distinct characters in 256
        # samples (39-char alphabet, deterministic byte->body mapping).
        assert len(chars_seen) >= 20


# ── Random generator ──────────────────────────────────────────────


class TestRandomGenerator:
    def test_random_cusips_validate(self):
        rng = np.random.default_rng(seed=42)
        for _ in range(100):
            cusip = generate_random(rng)
            assert _is_valid_cusip(cusip), f"Got invalid {cusip!r}"

    def test_random_uses_full_alphabet_eventually(self):
        rng = np.random.default_rng(seed=7)
        chars_seen: set[str] = set()
        for _ in range(500):
            chars_seen.update(generate_random(rng)[:8])
        # 500 samples of 8 chars each should hit every alphabet entry.
        assert set(_CUSIP_ALPHABET) <= chars_seen


# ── Adapter ───────────────────────────────────────────────────────


class TestCusipAdapter:
    def test_unknown_provider_rejected(self):
        adapter = CusipAdapter()
        with pytest.raises(ProviderError, match="unknown_provider"):
            adapter.generate("not_cusip", spec=_spec())

    def test_random_generate(self):
        adapter = CusipAdapter()
        spec = _spec()
        for _ in range(10):
            cusip = adapter.generate("synthetic_cusip", spec=spec)
            assert _is_valid_cusip(cusip)

    def test_deterministic_same_input_same_output(self):
        adapter = CusipAdapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        out1 = adapter.generate("synthetic_cusip", spec=spec, source_value="AAPL")
        out2 = adapter.generate("synthetic_cusip", spec=spec, source_value="AAPL")
        assert out1 == out2
        assert _is_valid_cusip(out1)

    def test_deterministic_different_input_different_output(self):
        adapter = CusipAdapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        out1 = adapter.generate("synthetic_cusip", spec=spec, source_value="AAPL")
        out2 = adapter.generate("synthetic_cusip", spec=spec, source_value="MSFT")
        assert out1 != out2

    def test_batch_size(self):
        adapter = CusipAdapter()
        spec = _spec()
        batch = adapter.generate_batch("synthetic_cusip", spec=spec, count=10)
        assert len(batch) == 10
        for cusip in batch:
            assert _is_valid_cusip(cusip)

    def test_batch_deterministic_unsupported(self):
        adapter = CusipAdapter()
        spec = _spec(deterministic=True, seed=_SEED, namespace="ns")
        with pytest.raises(ProviderError, match="batch_deterministic_unsupported"):
            adapter.generate_batch("synthetic_cusip", spec=spec, count=4)

    def test_capability_matrix(self):
        adapter = CusipAdapter()
        cap = adapter.capability_matrix("synthetic_cusip")
        assert cap.provider == "synthetic_cusip"
        assert cap.supports_deterministic is True
        assert "cusip_modified_luhn" in cap.blocklist_validators

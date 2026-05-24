"""
Tests for FPEStrategy — Sprint B · Item 32.

Covers:
  keyed determinism: same key + same input → same output across two instances
  different key or different input → different output
  format preservation: output has same length and all chars stay in charset
  separator preservation: non-charset characters stay at their original positions
  Luhn validation: last digit is the correct Luhn check digit
  NULL / NaN passthrough
  charset variants (digits, alpha, alphanum, explicit)
  factory wiring: create_strategy('fpe') returns FPEStrategy
  validator acceptance: 'fpe' in SUPPORTED_MASKING_STRATEGIES
  legacy fallback (no derive_key) still produces deterministic output
"""

import hashlib
import hmac

import pandas as pd

from decoy_engine.internal.validator import MaskerConfigValidator
from decoy_engine.transforms.factory import create_strategy
from decoy_engine.transforms.fpe import _CHARSETS, FPEStrategy

MASTER_KEY_A = b"test-master-key-A-32bytes-padded"
MASTER_KEY_B = b"test-master-key-B-32bytes-padded"


def _make_derive_key(master: bytes):
    """Simple derive_key factory mirroring the platform's make_key_resolver."""

    def derive_key(info: str) -> bytes:
        return hmac.new(master, info.encode(), hashlib.sha256).digest()

    return derive_key


# ---------------------------------------------------------------------------
# Keyed determinism
# ---------------------------------------------------------------------------


def test_keyed_same_input_same_output_across_instances():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "ssn", "type": "fpe", "charset": "digits"}
    col = pd.Series(["123456789", "987654321", "000000000"])
    out1 = FPEStrategy(derive_key=dk).apply(col, rule)
    out2 = FPEStrategy(derive_key=dk).apply(col, rule)
    pd.testing.assert_series_equal(out1, out2)


def test_keyed_different_master_different_output():
    dk_a = _make_derive_key(MASTER_KEY_A)
    dk_b = _make_derive_key(MASTER_KEY_B)
    rule = {"column": "ssn", "type": "fpe", "charset": "digits"}
    col = pd.Series(["123456789"])
    out_a = FPEStrategy(derive_key=dk_a).apply(col, rule)
    out_b = FPEStrategy(derive_key=dk_b).apply(col, rule)
    assert out_a[0] != out_b[0], "different master keys must produce different outputs"


def test_keyed_different_input_different_output():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "ssn", "type": "fpe", "charset": "digits"}
    col = pd.Series(["123456789", "123456788"])  # differ by one digit
    out = FPEStrategy(derive_key=dk).apply(col, rule)
    assert out[0] != out[1], "different inputs must produce different outputs"


# ---------------------------------------------------------------------------
# Format preservation
# ---------------------------------------------------------------------------


def test_preserves_length_digits():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "id", "type": "fpe", "charset": "digits"}
    values = ["0", "12", "123456789", "1234567890123456"]
    col = pd.Series(values)
    out = FPEStrategy(derive_key=dk).apply(col, rule)
    for orig, enc in zip(values, out, strict=False):
        assert len(enc) == len(orig), f"length changed: {orig!r} → {enc!r}"


def test_output_stays_in_digits_charset():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "id", "type": "fpe", "charset": "digits"}
    col = pd.Series(["123456789", "000000000", "999999999"])
    out = FPEStrategy(derive_key=dk).apply(col, rule)
    for enc in out:
        assert enc.isdigit(), f"non-digit character in output: {enc!r}"


def test_output_stays_in_alpha_charset():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "code", "type": "fpe", "charset": "alpha"}
    col = pd.Series(["abcdefgh", "zzzzzzzz"])
    out = FPEStrategy(derive_key=dk).apply(col, rule)
    for enc in out:
        assert enc.islower() and enc.isalpha(), f"output outside alpha charset: {enc!r}"


def test_output_stays_in_alphanum_charset():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "code", "type": "fpe", "charset": "alphanum"}
    charset = _CHARSETS["alphanum"]
    col = pd.Series(["abc123", "000zzz"])
    out = FPEStrategy(derive_key=dk).apply(col, rule)
    for orig, enc in zip(["abc123", "000zzz"], out, strict=False):
        assert all(c in charset for c in enc), f"char outside alphanum charset: {enc!r}"
        assert len(enc) == len(orig)


def test_explicit_charset():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "hex", "type": "fpe", "charset": "0123456789ABCDEF"}
    col = pd.Series(["DEADBEEF", "12345678"])
    out = FPEStrategy(derive_key=dk).apply(col, rule)
    for enc in out:
        assert all(c in "0123456789ABCDEF" for c in enc)
        assert len(enc) == 8


# ---------------------------------------------------------------------------
# Separator preservation
# ---------------------------------------------------------------------------


def test_preserves_ssn_dashes():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "ssn", "type": "fpe", "charset": "digits", "preserve_separators": True}
    col = pd.Series(["123-45-6789"])
    enc = FPEStrategy(derive_key=dk).apply(col, rule)[0]
    assert enc[3] == "-" and enc[6] == "-", f"dashes not preserved: {enc!r}"
    assert len(enc) == 11
    for i in [0, 1, 2, 4, 5, 7, 8, 9, 10]:
        assert enc[i].isdigit(), f"position {i} not a digit: {enc!r}"


def test_preserves_pan_dashes():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "pan", "type": "fpe", "charset": "digits", "preserve_separators": True}
    col = pd.Series(["4111-1111-1111-1111"])
    enc = FPEStrategy(derive_key=dk).apply(col, rule)[0]
    assert enc[4] == "-" and enc[9] == "-" and enc[14] == "-"
    assert len(enc) == 19


def test_separator_determinism():
    """Separator-preserved output is still deterministic across calls."""
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "ssn", "type": "fpe", "charset": "digits", "preserve_separators": True}
    col = pd.Series(["123-45-6789"])
    out1 = FPEStrategy(derive_key=dk).apply(col, rule)
    out2 = FPEStrategy(derive_key=dk).apply(col, rule)
    assert out1[0] == out2[0]


# ---------------------------------------------------------------------------
# Luhn validation
# ---------------------------------------------------------------------------


def _luhn_passes(s: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(s)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def test_validate_luhn_output_passes_checksum():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "pan", "type": "fpe", "charset": "digits", "validate_luhn": True}
    col = pd.Series(["4111111111111111", "5500005555555559", "1234567890123456"])
    out = FPEStrategy(derive_key=dk).apply(col, rule)
    for enc in out:
        assert _luhn_passes(enc), f"Luhn check failed for {enc!r}"


def test_validate_luhn_preserves_length():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "pan", "type": "fpe", "charset": "digits", "validate_luhn": True}
    col = pd.Series(["1234567890123456"])
    out = FPEStrategy(derive_key=dk).apply(col, rule)
    assert len(out[0]) == 16


def test_luhn_ignored_for_non_digit_charset():
    """validate_luhn is silently ignored when charset has non-digit characters."""
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "code", "type": "fpe", "charset": "alphanum", "validate_luhn": True}
    col = pd.Series(["abc123def456"])
    charset = _CHARSETS["alphanum"]
    enc = FPEStrategy(derive_key=dk).apply(col, rule)[0]
    # Should not raise; output still in charset
    assert all(c in charset for c in enc)
    assert len(enc) == 12


# ---------------------------------------------------------------------------
# NULL / NaN passthrough
# ---------------------------------------------------------------------------


def test_null_passthrough():
    dk = _make_derive_key(MASTER_KEY_A)
    rule = {"column": "ssn", "type": "fpe", "charset": "digits"}
    col = pd.Series(["123456789", None, float("nan"), "987654321"])
    out = FPEStrategy(derive_key=dk).apply(col, rule)
    assert out[1] is None or pd.isna(out[1])
    assert pd.isna(out[2])
    assert out[0].isdigit() and len(out[0]) == 9
    assert out[3].isdigit() and len(out[3]) == 9


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------


def test_factory_returns_fpe_strategy():
    assert isinstance(create_strategy("fpe"), FPEStrategy)


def test_factory_passes_derive_key():
    dk = _make_derive_key(MASTER_KEY_A)
    s = create_strategy("fpe", derive_key=dk)
    assert isinstance(s, FPEStrategy)
    assert s.derive_key is dk


# ---------------------------------------------------------------------------
# Validator acceptance
# ---------------------------------------------------------------------------


def test_validator_accepts_fpe_strategy():
    validator = MaskerConfigValidator()
    config = {
        "input": {"type": "csv", "path": "input.csv"},
        "output": {"type": "csv", "path": "output.csv"},
        "masking_rules": [{"column": "ssn", "type": "fpe"}],
    }
    validator.validate(config)  # must not raise


def test_validator_accepts_fpe_with_options():
    validator = MaskerConfigValidator()
    config = {
        "input": {"type": "csv", "path": "input.csv"},
        "output": {"type": "csv", "path": "output.csv"},
        "masking_rules": [
            {
                "column": "pan",
                "type": "fpe",
                "charset": "digits",
                "preserve_separators": True,
                "validate_luhn": True,
            }
        ],
    }
    validator.validate(config)  # must not raise


# ---------------------------------------------------------------------------
# Legacy fallback (no derive_key)
# ---------------------------------------------------------------------------


def test_legacy_deterministic_same_seed():
    rule = {"column": "ssn", "type": "fpe", "charset": "digits"}
    col = pd.Series(["123456789"])
    out1 = FPEStrategy(seed=42).apply(col, rule)
    out2 = FPEStrategy(seed=42).apply(col, rule)
    assert out1[0] == out2[0]


def test_legacy_different_seed_different_output():
    rule = {"column": "ssn", "type": "fpe", "charset": "digits"}
    col = pd.Series(["123456789"])
    out1 = FPEStrategy(seed=42).apply(col, rule)
    out2 = FPEStrategy(seed=99).apply(col, rule)
    assert out1[0] != out2[0]


def test_legacy_output_format_preserved():
    rule = {"column": "ssn", "type": "fpe", "charset": "digits"}
    col = pd.Series(["123456789"])
    out = FPEStrategy(seed=42).apply(col, rule)
    assert out[0].isdigit() and len(out[0]) == 9

"""FPE decrypt round-trip (capability-gaps WS1, 2026-06-12).

The Feistel permutation has always been a bijection; these tests pin the
newly added inverse: `fpe_decrypt_value(fpe_encrypt_value(x)) == x` for
every config shape (plain, preserve_separators, validate_luhn, single
char, custom charset). Luhn mode round-trips exactly when the source is
Luhn-valid (the check digit is recomputed, not stored), which is the
domain Luhn mode exists for.
"""

from __future__ import annotations

import pytest

from decoy_engine.transforms.fpe import (
    _CHARSETS,
    _luhn_check_digit,
    fpe_decrypt_value,
    fpe_encrypt_value,
)

_KEY = bytes(range(32))
_TWEAK = b"card_number"


def _luhn_valid(digits: str) -> str:
    return digits[:-1] + _luhn_check_digit(digits[:-1])


class TestPlainRoundTrip:
    @pytest.mark.parametrize(
        "value,charset",
        [
            ("123456789", "digits"),
            ("00000000", "digits"),
            ("hello", "alpha"),
            ("ABCXYZ", "ALPHA"),
            ("a1b2c3", "alphanum"),
            ("Mixed123Case", "ALPHANUM"),
            ("42", "digits"),
        ],
    )
    def test_decrypt_inverts_encrypt(self, value: str, charset: str) -> None:
        cs = _CHARSETS[charset]
        enc = fpe_encrypt_value(value, _KEY, cs, _TWEAK)
        assert enc != value  # vanishingly unlikely fixed point would mask a no-op bug
        assert len(enc) == len(value)
        assert fpe_decrypt_value(enc, _KEY, cs, _TWEAK) == value

    def test_single_character(self) -> None:
        cs = _CHARSETS["digits"]
        for ch in cs:
            enc = fpe_encrypt_value(ch, _KEY, cs, _TWEAK)
            assert fpe_decrypt_value(enc, _KEY, cs, _TWEAK) == ch

    def test_custom_charset(self) -> None:
        cs = "xyz123"
        enc = fpe_encrypt_value("zzz111", _KEY, cs, _TWEAK)
        assert fpe_decrypt_value(enc, _KEY, cs, _TWEAK) == "zzz111"

    def test_wrong_key_does_not_invert(self) -> None:
        cs = _CHARSETS["digits"]
        enc = fpe_encrypt_value("123456789", _KEY, cs, _TWEAK)
        assert fpe_decrypt_value(enc, b"\x00" * 32, cs, _TWEAK) != "123456789"

    def test_wrong_tweak_does_not_invert(self) -> None:
        cs = _CHARSETS["digits"]
        enc = fpe_encrypt_value("123456789", _KEY, cs, _TWEAK)
        assert fpe_decrypt_value(enc, _KEY, cs, b"other_column") != "123456789"

    def test_empty_string_passes_through(self) -> None:
        cs = _CHARSETS["digits"]
        assert fpe_encrypt_value("", _KEY, cs, _TWEAK) == ""
        assert fpe_decrypt_value("", _KEY, cs, _TWEAK) == ""


class TestSeparatorRoundTrip:
    def test_separators_stay_in_place_both_directions(self) -> None:
        cs = _CHARSETS["digits"]
        val = "123-45-6789"
        enc = fpe_encrypt_value(val, _KEY, cs, _TWEAK, preserve_separators=True)
        assert enc[3] == "-" and enc[6] == "-"
        assert enc != val
        dec = fpe_decrypt_value(enc, _KEY, cs, _TWEAK, preserve_separators=True)
        assert dec == val

    def test_no_charset_chars_passes_through(self) -> None:
        cs = _CHARSETS["digits"]
        assert fpe_encrypt_value("---", _KEY, cs, _TWEAK, preserve_separators=True) == "---"
        assert fpe_decrypt_value("---", _KEY, cs, _TWEAK, preserve_separators=True) == "---"


class TestLuhnRoundTrip:
    def test_luhn_valid_pan_round_trips_exactly(self) -> None:
        cs = _CHARSETS["digits"]
        pan = _luhn_valid("4532015112830361")
        enc = fpe_encrypt_value(pan, _KEY, cs, _TWEAK, validate_luhn=True)
        # Output is itself Luhn-valid (the point of the mode).
        assert enc[-1] == _luhn_check_digit(enc[:-1])
        assert enc != pan
        dec = fpe_decrypt_value(enc, _KEY, cs, _TWEAK, validate_luhn=True)
        assert dec == pan

    def test_luhn_invalid_source_recovers_body_and_normalizes_check_digit(self) -> None:
        """The check digit is recomputed on decrypt, not stored: a source
        that violated Luhn comes back with every digit except the last
        intact and the last digit corrected to the Luhn check digit."""
        cs = _CHARSETS["digits"]
        body = "453201511283036"
        bad = body + "9"
        assert _luhn_check_digit(body) != "9"
        enc = fpe_encrypt_value(bad, _KEY, cs, _TWEAK, validate_luhn=True)
        dec = fpe_decrypt_value(enc, _KEY, cs, _TWEAK, validate_luhn=True)
        assert dec[:-1] == body
        assert dec == body + _luhn_check_digit(body)

    def test_luhn_with_separators(self) -> None:
        cs = _CHARSETS["digits"]
        pan = _luhn_valid("4532015112830361")
        spaced = f"{pan[:4]} {pan[4:8]} {pan[8:12]} {pan[12:]}"
        enc = fpe_encrypt_value(
            spaced, _KEY, cs, _TWEAK, preserve_separators=True, validate_luhn=True
        )
        assert enc.count(" ") == 3
        dec = fpe_decrypt_value(
            enc, _KEY, cs, _TWEAK, preserve_separators=True, validate_luhn=True
        )
        assert dec == spaced


class TestDeterminism:
    def test_encrypt_is_stable(self) -> None:
        cs = _CHARSETS["digits"]
        a = fpe_encrypt_value("987654321", _KEY, cs, _TWEAK)
        b = fpe_encrypt_value("987654321", _KEY, cs, _TWEAK)
        assert a == b

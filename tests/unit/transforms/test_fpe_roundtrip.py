"""FPE decrypt round-trip (capability-gaps WS1, 2026-06-12).

FF1 (Task 5.2) is a bijection over its admissible domain; these tests pin
the inverse: `fpe_decrypt_value(fpe_encrypt_value(x)) == x` for every
config shape (plain, preserve_separators, validate_luhn, custom charset).
Every example value is chosen so its in-charset length clears the FF1
minimum admissible domain (radix ** length >= 1,000,000) for its charset;
below that floor the engine fails closed instead of encrypting (see
`test_fpe_fail_closed_pipeline.py` / `test_ff1_primitive.py` for that
behavior). Luhn mode round-trips exactly when the source is Luhn-valid
(the check digit is recomputed, not stored), which is the domain Luhn mode
exists for.
"""

from __future__ import annotations

import pytest

from decoy_engine.errors import FpeUnencryptableError
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


_PLAIN_ROUNDTRIP_CASES = [
    ("123456789", "digits"),
    ("00000000", "digits"),
    ("424242", "digits"),  # radix 10, length 6: exactly at the FF1 floor (10**6)
    ("hello", "alpha"),
    ("ABCXYZ", "ALPHA"),
    ("a1b2c3", "alphanum"),
    ("Mixed123Case", "ALPHANUM"),
]


class TestPlainRoundTrip:
    @pytest.mark.parametrize("value,charset", _PLAIN_ROUNDTRIP_CASES)
    def test_decrypt_inverts_encrypt(self, value: str, charset: str) -> None:
        cs = _CHARSETS[charset]
        enc = fpe_encrypt_value(value, _KEY, cs, _TWEAK)
        assert len(enc) == len(value)
        assert fpe_decrypt_value(enc, _KEY, cs, _TWEAK) == value

    def test_encrypt_is_not_a_global_no_op(self) -> None:
        """A fixed point (ciphertext == plaintext) is legal for a single
        permutation input, so per-case inequality is not a valid
        invariant; a strategy that is ALWAYS a no-op is the real bug this
        guards against. Across the whole roundtrip case list under one
        fixed key/tweak, at least one output must differ from its input."""
        outputs = [
            fpe_encrypt_value(value, _KEY, _CHARSETS[charset], _TWEAK)
            for value, charset in _PLAIN_ROUNDTRIP_CASES
        ]
        assert any(
            enc != value
            for enc, (value, _charset) in zip(outputs, _PLAIN_ROUNDTRIP_CASES, strict=True)
        ), "every roundtrip case reproduced its plaintext unchanged: the strategy is a no-op"

    def test_single_character_fails_closed_on_domain_floor(self) -> None:
        """FF1 has no single-character case: the algorithm itself requires a
        numeral string of length >= 2, and even disregarding that, one
        in-charset character is always below the FF1 minimum admissible
        domain for every charset this engine ships (max radix 62, and
        62**1 << 1,000,000). A single character now fails closed instead of
        permuting."""
        from decoy_engine.errors import FpeUnencryptableError

        cs = _CHARSETS["digits"]
        for ch in cs:
            with pytest.raises(FpeUnencryptableError) as exc:
                fpe_encrypt_value(ch, _KEY, cs, _TWEAK)
            assert exc.value.code == "fpe.unencryptable_domain"

    def test_custom_charset(self) -> None:
        # radix 6, length 8: 6**8 = 1,679,616, clears the FF1 floor.
        cs = "xyz123"
        enc = fpe_encrypt_value("zzz111zz", _KEY, cs, _TWEAK)
        assert fpe_decrypt_value(enc, _KEY, cs, _TWEAK) == "zzz111zz"

    def test_wrong_key_does_not_invert(self) -> None:
        """A single wrong key recovering the plaintext by coincidence is
        legal for a permutation (domain >= 1e6 makes it rare, not
        impossible), so the invariant is aggregate across several distinct
        wrong keys rather than a universal claim about one."""
        cs = _CHARSETS["digits"]
        source = "123456789"
        enc = fpe_encrypt_value(source, _KEY, cs, _TWEAK)
        wrong_keys = [bytes([i]) * 32 for i in range(8) if bytes([i]) * 32 != _KEY]
        recovered = [fpe_decrypt_value(enc, wrong_key, cs, _TWEAK) for wrong_key in wrong_keys]
        assert any(r != source for r in recovered), (
            "every wrong key recovered the correct plaintext: decryption does not depend on the key"
        )

    def test_wrong_tweak_does_not_invert(self) -> None:
        """Same reasoning as the wrong-key case above: aggregate across
        several distinct wrong tweaks rather than a universal per-tweak
        claim."""
        cs = _CHARSETS["digits"]
        source = "123456789"
        enc = fpe_encrypt_value(source, _KEY, cs, _TWEAK)
        wrong_tweaks = [f"other_column_{i}".encode() for i in range(8)]
        recovered = [fpe_decrypt_value(enc, _KEY, cs, wrong_tweak) for wrong_tweak in wrong_tweaks]
        assert any(r != source for r in recovered), (
            "every wrong tweak recovered the correct plaintext: decryption does not "
            "depend on the tweak"
        )

    def test_empty_string_fails_closed(self) -> None:
        """Plan P2: an empty value is not a null (nulls are filtered one layer
        up); its domain is below the FF1 floor, so it fails closed like any
        other sub-floor value rather than passing through as a no-op."""
        cs = _CHARSETS["digits"]
        with pytest.raises(FpeUnencryptableError) as enc_exc:
            fpe_encrypt_value("", _KEY, cs, _TWEAK)
        assert enc_exc.value.code == "fpe.unencryptable_domain"
        with pytest.raises(FpeUnencryptableError) as dec_exc:
            fpe_decrypt_value("", _KEY, cs, _TWEAK)
        assert dec_exc.value.code == "fpe.unencryptable_domain"


class TestSeparatorRoundTrip:
    def test_separators_stay_in_place_both_directions(self) -> None:
        cs = _CHARSETS["digits"]
        val = "123-45-6789"
        enc = fpe_encrypt_value(val, _KEY, cs, _TWEAK, preserve_separators=True)
        assert enc[3] == "-" and enc[6] == "-"
        dec = fpe_decrypt_value(enc, _KEY, cs, _TWEAK, preserve_separators=True)
        assert dec == val

    def test_no_charset_chars_fails_closed(self) -> None:
        """DE-01 cluster-C: all-out-of-charset values FAIL CLOSED (supersedes fix #42).

        Fix #42 replaced the verbatim passthrough with a covering hash, but that
        hash is non-invertible, so the "reversible" column silently did not
        round-trip (`'---' -> '092' -> '858'`). The engine now raises rather than
        emit a value that cannot be recovered."""
        from decoy_engine.errors import FpeUnencryptableError

        cs = _CHARSETS["digits"]
        with pytest.raises(FpeUnencryptableError):
            fpe_encrypt_value("---", _KEY, cs, _TWEAK, preserve_separators=True)


class TestLuhnRoundTrip:
    def test_luhn_valid_pan_round_trips_exactly(self) -> None:
        cs = _CHARSETS["digits"]
        pan = _luhn_valid("4532015112830361")
        enc = fpe_encrypt_value(pan, _KEY, cs, _TWEAK, validate_luhn=True)
        # Output is itself Luhn-valid (the point of the mode).
        assert enc[-1] == _luhn_check_digit(enc[:-1])
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
        dec = fpe_decrypt_value(enc, _KEY, cs, _TWEAK, preserve_separators=True, validate_luhn=True)
        assert dec == spaced


class TestDeterminism:
    def test_encrypt_is_stable(self) -> None:
        cs = _CHARSETS["digits"]
        a = fpe_encrypt_value("987654321", _KEY, cs, _TWEAK)
        b = fpe_encrypt_value("987654321", _KEY, cs, _TWEAK)
        assert a == b

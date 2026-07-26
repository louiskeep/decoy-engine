"""TDD tests for FPE checksum: mode - SP-04 remediation (P5.INFRA.1).

Each supported scheme must produce valid-by-construction output.
IBAN and unknown schemes must FAIL CLOSED (raise).
These tests were written BEFORE the fixes and drive the implementation.

Findings:
  M1 - NPI: pin leading digit; validate enforces 1/2 rule
  H1 - VIN: constrain to VIN alphabet; no KeyError for I/O/Q in charset
  B2 - isbn13: pin bookland prefix 978/979
  luhn/ean13/gtin: digits-only charset; output validates
  B1 - IBAN: raises typed error (per-country BBAN cannot be FPE-masked)
  H2 - unknown scheme: raises at _fpe_checksum_permute + plan-compile
  L1 - per-scheme minimum length: too-short handled cleanly
"""

from __future__ import annotations

import pytest

import decoy_engine.checksums as checksums
from decoy_engine.errors import FpeChecksumError
from decoy_engine.transforms.fpe import fpe_decrypt_value, fpe_encrypt_value

_KEY = bytes(range(32))
_TWEAK = b"test_col"
_DIGITS = "0123456789"
_VIN_ALPHA = "0123456789ABCDEFGHJKLMNPRSTUVWXYZ"  # VIN alphabet (no I/O/Q)


# ---------------------------------------------------------------------------
# M1: NPI - valid by construction + leading digit preserved
# ---------------------------------------------------------------------------


class TestFpeNpi:
    """FPE with checksum='npi' must produce a checksum-valid NPI starting
    with 1 or 2 (NPPES allocation rule, CMS 2008)."""

    _NPIS = ["1234567893", "1679576722", "1000000004", "2000000002"]

    def test_fpe_npi_output_validates_via_checksums(self) -> None:
        for npi in self._NPIS:
            out = fpe_encrypt_value(npi, _KEY, _DIGITS, _TWEAK, checksum="npi")
            assert checksums.validate("npi", out), (
                f"FPE output {out!r} (from {npi!r}) failed checksums.validate('npi')"
            )

    def test_fpe_npi_output_validates_via_engine_validator(self) -> None:
        from decoy_engine.providers_v2.identifiers._npi import NpiValidator

        for npi in self._NPIS:
            out = fpe_encrypt_value(npi, _KEY, _DIGITS, _TWEAK, checksum="npi")
            assert NpiValidator.is_valid(out), (
                f"FPE output {out!r} (from {npi!r}) failed NpiValidator.is_valid"
            )

    def test_fpe_npi_preserves_leading_digit(self) -> None:
        for npi in self._NPIS:
            out = fpe_encrypt_value(npi, _KEY, _DIGITS, _TWEAK, checksum="npi")
            assert out[0] in ("1", "2"), (
                f"FPE output {out!r} lost leading-digit constraint (from {npi!r})"
            )

    def test_fpe_npi_correct_length(self) -> None:
        out = fpe_encrypt_value("1234567893", _KEY, _DIGITS, _TWEAK, checksum="npi")
        assert len(out) == 10 and out.isdigit()

    def test_fpe_npi_deterministic(self) -> None:
        a = fpe_encrypt_value("1234567893", _KEY, _DIGITS, _TWEAK, checksum="npi")
        b = fpe_encrypt_value("1234567893", _KEY, _DIGITS, _TWEAK, checksum="npi")
        assert a == b


# ---------------------------------------------------------------------------
# H1: VIN - valid by construction + no KeyError for I/O/Q in charset
# ---------------------------------------------------------------------------


class TestFpeVin:
    """FPE with checksum='vin' must produce a check-digit-valid 17-char VIN
    and must not KeyError even when the caller's charset includes I, O, or Q."""

    _VIN = "1HGCM82633A004352"

    def test_fpe_vin_output_validates(self) -> None:
        out = fpe_encrypt_value(self._VIN, _KEY, _VIN_ALPHA, _TWEAK, checksum="vin")
        assert checksums.validate("vin", out), (
            f"FPE VIN output {out!r} failed checksums.validate('vin')"
        )

    def test_fpe_vin_no_keyerror_with_ioq_in_charset(self) -> None:
        bad_charset = _VIN_ALPHA + "IOQ"
        out = fpe_encrypt_value(self._VIN, _KEY, bad_charset, _TWEAK, checksum="vin")
        assert checksums.validate("vin", out), (
            f"FPE VIN output {out!r} failed validate after I/O/Q-charset call"
        )

    def test_fpe_vin_correct_length(self) -> None:
        out = fpe_encrypt_value(self._VIN, _KEY, _VIN_ALPHA, _TWEAK, checksum="vin")
        assert len(out) == 17

    def test_fpe_vin_deterministic(self) -> None:
        a = fpe_encrypt_value(self._VIN, _KEY, _VIN_ALPHA, _TWEAK, checksum="vin")
        b = fpe_encrypt_value(self._VIN, _KEY, _VIN_ALPHA, _TWEAK, checksum="vin")
        assert a == b

    def test_fpe_vin_narrow_charset_falls_back_to_digits(self) -> None:
        # When the caller's charset filters to fewer than two VIN-alphabet
        # characters the body permutation falls back to the digit subset so it
        # can still proceed. A degenerate all-'0' value with a single-char
        # charset exercises that fallback; a null fallback charset would break
        # the permutation instead of producing a valid VIN.
        out = fpe_encrypt_value("0" * 17, _KEY, "0", _TWEAK, checksum="vin")
        assert len(out) == 17
        assert checksums.validate("vin", out)


# ---------------------------------------------------------------------------
# B2: isbn13 - valid by construction + bookland prefix preserved
# ---------------------------------------------------------------------------


class TestFpeIsbn13:
    """FPE with checksum='isbn13' must produce an isbn13-valid 13-char string
    and must preserve the 978/979 bookland prefix."""

    _ISBNS = ["9783161484100", "9780471117094", "9790201234567"]

    def test_fpe_isbn13_output_validates(self) -> None:
        for isbn in self._ISBNS:
            out = fpe_encrypt_value(isbn, _KEY, _DIGITS, _TWEAK, checksum="isbn13")
            assert checksums.validate("isbn13", out), (
                f"FPE isbn13 output {out!r} (from {isbn!r}) failed validate"
            )

    def test_fpe_isbn13_preserves_bookland_prefix(self) -> None:
        for isbn in self._ISBNS:
            out = fpe_encrypt_value(isbn, _KEY, _DIGITS, _TWEAK, checksum="isbn13")
            assert out[:3] in ("978", "979"), (
                f"FPE isbn13 output {out!r} lost bookland prefix (from {isbn!r})"
            )

    def test_fpe_isbn13_correct_length(self) -> None:
        out = fpe_encrypt_value("9783161484100", _KEY, _DIGITS, _TWEAK, checksum="isbn13")
        assert len(out) == 13 and out.isdigit()

    def test_fpe_isbn13_deterministic(self) -> None:
        a = fpe_encrypt_value("9783161484100", _KEY, _DIGITS, _TWEAK, checksum="isbn13")
        b = fpe_encrypt_value("9783161484100", _KEY, _DIGITS, _TWEAK, checksum="isbn13")
        assert a == b


# ---------------------------------------------------------------------------
# luhn / ean13 / gtin - valid by construction
# ---------------------------------------------------------------------------


class TestFpeLuhn:
    def test_fpe_luhn_output_validates(self) -> None:
        pan = "4532015112830366"
        out = fpe_encrypt_value(pan, _KEY, _DIGITS, _TWEAK, checksum="luhn")
        assert checksums.validate("luhn", out), f"FPE luhn output {out!r} failed validate"

    def test_fpe_luhn_deterministic(self) -> None:
        a = fpe_encrypt_value("4532015112830366", _KEY, _DIGITS, _TWEAK, checksum="luhn")
        b = fpe_encrypt_value("4532015112830366", _KEY, _DIGITS, _TWEAK, checksum="luhn")
        assert a == b


class TestFpeEan13:
    def test_fpe_ean13_output_validates(self) -> None:
        ean = "4006381333931"
        out = fpe_encrypt_value(ean, _KEY, _DIGITS, _TWEAK, checksum="ean13")
        assert checksums.validate("ean13", out), f"FPE ean13 output {out!r} failed validate"

    def test_fpe_ean13_deterministic(self) -> None:
        a = fpe_encrypt_value("4006381333931", _KEY, _DIGITS, _TWEAK, checksum="ean13")
        b = fpe_encrypt_value("4006381333931", _KEY, _DIGITS, _TWEAK, checksum="ean13")
        assert a == b


class TestFpeGtin:
    def test_fpe_gtin14_output_validates(self) -> None:
        gtin = "98412345678908"
        out = fpe_encrypt_value(gtin, _KEY, _DIGITS, _TWEAK, checksum="gtin")
        assert checksums.validate("gtin", out), f"FPE gtin output {out!r} failed validate"

    def test_fpe_gtin_deterministic(self) -> None:
        a = fpe_encrypt_value("98412345678908", _KEY, _DIGITS, _TWEAK, checksum="gtin")
        b = fpe_encrypt_value("98412345678908", _KEY, _DIGITS, _TWEAK, checksum="gtin")
        assert a == b


# ---------------------------------------------------------------------------
# B1: IBAN - FAIL CLOSED (must raise, not emit)
# ---------------------------------------------------------------------------


class TestFpeIbanFailClosed:
    """FPE with checksum='iban' must raise a typed error.

    Per-country BBAN structure cannot be preserved through a free Feistel
    permutation; the stdnum.iban validator enforces country-specific regexes.
    Decoy refuses to emit structurally invalid IBANs silently.
    """

    def test_fpe_iban_raises_typed_error(self) -> None:
        iban = "GB82WEST12345698765432"
        with pytest.raises(FpeChecksumError, match="iban"):
            fpe_encrypt_value(
                iban, _KEY, "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", _TWEAK, checksum="iban"
            )

    def test_fpe_iban_does_not_emit_value(self) -> None:
        iban = "GB82WEST12345698765432"
        try:
            result = fpe_encrypt_value(
                iban, _KEY, "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", _TWEAK, checksum="iban"
            )
            pytest.fail(f"Expected an exception but got {result!r}")
        except FpeChecksumError:
            pass  # expected


# ---------------------------------------------------------------------------
# H2: Unknown scheme - FAIL CLOSED at _fpe_checksum_permute
# ---------------------------------------------------------------------------


class TestFpeUnknownSchemeFailClosed:
    """FPE with an unknown checksum scheme must raise immediately.

    Decoy forbids silent misconfiguration passthrough.
    The 'validation upstream' comment in the old code was false; an
    unrecognised scheme silently fell through to plain FPE with no check digit.
    """

    def test_unknown_scheme_raises_in_fpe(self) -> None:
        with pytest.raises(FpeChecksumError, match="bogus|unknown"):
            fpe_encrypt_value("12345", _KEY, _DIGITS, _TWEAK, checksum="bogus")

    def test_typo_scheme_raises_in_fpe(self) -> None:
        with pytest.raises(FpeChecksumError, match="ibna|unknown"):
            fpe_encrypt_value("12345", _KEY, _DIGITS, _TWEAK, checksum="ibna")


# ---------------------------------------------------------------------------
# L1: Per-scheme minimum length guard
# ---------------------------------------------------------------------------


class TestFpeMinimumLength:
    """Too-short values for structured schemes FAIL CLOSED (DE-01 cluster-C).

    Pre-fix, a value below a scheme's minimum length (`NPI<10`, `ISBN13<13`,
    `VIN<17`) was returned UNCHANGED -- a silent cleartext pass-through of an
    identifier the config asked to mask. The engine now raises `FpeChecksumError`
    rather than leak the value.
    """

    def test_npi_too_short_raises(self) -> None:
        short = "12345"  # 5 chars, not >= 10
        with pytest.raises(FpeChecksumError, match="npi"):
            fpe_encrypt_value(short, _KEY, _DIGITS, _TWEAK, checksum="npi")

    def test_vin_too_short_raises(self) -> None:
        short = "1HGCM826"  # 8 chars, not 17
        with pytest.raises(FpeChecksumError, match="vin"):
            fpe_encrypt_value(short, _KEY, _VIN_ALPHA, _TWEAK, checksum="vin")

    def test_isbn13_too_short_raises(self) -> None:
        short = "97831614"  # 8 chars, not 13
        with pytest.raises(FpeChecksumError, match="isbn13"):
            fpe_encrypt_value(short, _KEY, _DIGITS, _TWEAK, checksum="isbn13")


# ---------------------------------------------------------------------------
# Plan-compile: H2 unknown scheme raises at compile
# ---------------------------------------------------------------------------


class TestFpeChecksumCompileCheck:
    """checksum with a typo/unknown scheme must be caught at plan-compile time."""

    def _config_with_checksum(self, checksum: str) -> dict:
        return {
            "tables": [
                {
                    "name": "patients",
                    "columns": [
                        {
                            "name": "npi",
                            "strategy": "fpe",
                            "namespace": "patients_ns",
                            "provider_config": {"charset": "digits", "checksum": checksum},
                        }
                    ],
                }
            ]
        }

    def test_unknown_checksum_raises_at_compile(self) -> None:
        from decoy_engine.plan._checks import check_fpe_checksum_scheme
        from decoy_engine.plan._errors import PlanCompileError

        with pytest.raises(PlanCompileError, match="bogus|unknown|fpe_checksum"):
            check_fpe_checksum_scheme(self._config_with_checksum("bogus"))

    def test_iban_checksum_raises_at_compile(self) -> None:
        from decoy_engine.plan._checks import check_fpe_checksum_scheme
        from decoy_engine.plan._errors import PlanCompileError

        with pytest.raises(PlanCompileError, match="iban|fpe_checksum"):
            check_fpe_checksum_scheme(self._config_with_checksum("iban"))

    def test_known_non_iban_checksum_passes(self) -> None:
        from decoy_engine.plan._checks import check_fpe_checksum_scheme

        # npi and luhn are digit schemes; "digits" charset is correct.
        check_fpe_checksum_scheme(self._config_with_checksum("npi"))
        check_fpe_checksum_scheme(self._config_with_checksum("luhn"))
        # VIN requires the full VIN alphabet (digits + A-Z minus I/O/Q).
        # "digits" alone is incompatible; use an explicit VIN-compatible charset.
        check_fpe_checksum_scheme(
            {
                "tables": [
                    {
                        "name": "vehicles",
                        "columns": [
                            {
                                "name": "vin",
                                "strategy": "fpe",
                                "provider_config": {
                                    "charset": "0123456789ABCDEFGHJKLMNPRSTUVWXYZ",
                                    "checksum": "vin",
                                },
                            }
                        ],
                    }
                ]
            }
        )

    def test_no_checksum_passes(self) -> None:
        from decoy_engine.plan._checks import check_fpe_checksum_scheme

        config = {
            "tables": [
                {
                    "name": "patients",
                    "columns": [
                        {
                            "name": "npi",
                            "strategy": "fpe",
                            "namespace": "patients_ns",
                            "provider_config": {"charset": "digits"},
                        }
                    ],
                }
            ]
        }
        check_fpe_checksum_scheme(config)


# ---------------------------------------------------------------------------
# Charset-vs-scheme compatibility: fail-closed at compile (SP-04 remediation)
# ---------------------------------------------------------------------------


class TestFpeChecksumCharsetIncompatible:
    """charset incompatible with checksum scheme must raise at plan-compile.

    The hole: digits-only charset for VIN fragments the value via
    preserve_separators into short runs that fall below the L1 min-length
    guard, returning the input verbatim (unmasked).  The compile check
    prevents that silent passthrough from reaching runtime.
    """

    def _cfg(self, checksum: str, charset: str) -> dict:
        return {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "col",
                            "strategy": "fpe",
                            "provider_config": {
                                "charset": charset,
                                "checksum": checksum,
                            },
                        }
                    ],
                }
            ]
        }

    # RED tests: written before the check exists; must fail until implemented.

    def test_vin_digits_only_charset_raises_at_compile(self) -> None:
        """Digits-only charset for VIN must raise fpe_checksum_charset_incompatible.

        Before this fix: fpe_encrypt_value("1HGCM82633A004352", key,
        "0123456789", tweak, checksum="vin") returns the input verbatim.
        After this fix: the misconfiguration is caught at plan-compile and
        never reaches runtime.
        """
        from decoy_engine.plan._checks import check_fpe_checksum_scheme
        from decoy_engine.plan._errors import PlanCompileError

        with pytest.raises(PlanCompileError) as exc:
            check_fpe_checksum_scheme(self._cfg("vin", "0123456789"))
        assert exc.value.code == "fpe_checksum_charset_incompatible"
        assert "vin" in exc.value.message.lower()

    def test_luhn_alpha_only_charset_raises_at_compile(self) -> None:
        """Alpha-only charset for luhn (no digits) must raise fpe_checksum_charset_incompatible."""
        from decoy_engine.plan._checks import check_fpe_checksum_scheme
        from decoy_engine.plan._errors import PlanCompileError

        with pytest.raises(PlanCompileError) as exc:
            check_fpe_checksum_scheme(self._cfg("luhn", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        assert exc.value.code == "fpe_checksum_charset_incompatible"
        assert "luhn" in exc.value.message.lower()

    # Positive / no-over-rejection tests: valid configs must compile clean.

    def test_vin_exact_vin_alphabet_passes(self) -> None:
        """Charset equal to the VIN alphabet (no I/O/Q) must compile."""
        from decoy_engine.plan._checks import check_fpe_checksum_scheme

        check_fpe_checksum_scheme(self._cfg("vin", "0123456789ABCDEFGHJKLMNPRSTUVWXYZ"))

    def test_vin_superset_charset_passes(self) -> None:
        """Full uppercase alphanum (superset: includes I/O/Q, harmless extras) must compile."""
        from decoy_engine.plan._checks import check_fpe_checksum_scheme

        check_fpe_checksum_scheme(self._cfg("vin", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"))

    def test_vin_named_ALPHANUM_passes(self) -> None:
        """Named 'ALPHANUM' charset is a superset of the VIN alphabet; must compile."""
        from decoy_engine.plan._checks import check_fpe_checksum_scheme

        check_fpe_checksum_scheme(self._cfg("vin", "ALPHANUM"))

    def test_luhn_digits_charset_passes(self) -> None:
        """Digits charset is correct for luhn; must compile."""
        from decoy_engine.plan._checks import check_fpe_checksum_scheme

        check_fpe_checksum_scheme(self._cfg("luhn", "0123456789"))

    def test_luhn_named_digits_passes(self) -> None:
        """Named 'digits' charset for luhn; must compile."""
        from decoy_engine.plan._checks import check_fpe_checksum_scheme

        check_fpe_checksum_scheme(self._cfg("luhn", "digits"))

    def test_luhn_alphanum_superset_passes(self) -> None:
        """Alphanumeric superset (includes digits) for luhn; must compile."""
        from decoy_engine.plan._checks import check_fpe_checksum_scheme

        check_fpe_checksum_scheme(self._cfg("luhn", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"))


# ---------------------------------------------------------------------------
# Round-trip: decrypt inverts encrypt for a checksum-valid source
# ---------------------------------------------------------------------------


class TestFpeChecksumRoundTrip:
    """A checksum-valid source must survive encrypt then decrypt byte-exactly.

    Validity alone (output passes the scheme validator) is weaker than the real
    contract: the check digit is recomputed from whatever body the permutation
    produced, so a body permuted in the WRONG direction still validates. Only
    the round-trip pins direction -- if the forward flag stops reaching
    ``_permute`` (both directions collapse to one), decrypt no longer inverts
    encrypt and the source is not recovered. It also catches a corrupted
    permutation alphabet, which validates by construction but is not invertible.
    """

    def test_luhn_roundtrip(self) -> None:
        src = "4532015112830366"
        enc = fpe_encrypt_value(src, _KEY, _DIGITS, _TWEAK, checksum="luhn")
        assert fpe_decrypt_value(enc, _KEY, _DIGITS, _TWEAK, checksum="luhn") == src

    def test_ean13_roundtrip(self) -> None:
        src = "4006381333931"
        enc = fpe_encrypt_value(src, _KEY, _DIGITS, _TWEAK, checksum="ean13")
        assert fpe_decrypt_value(enc, _KEY, _DIGITS, _TWEAK, checksum="ean13") == src

    def test_gtin_roundtrip(self) -> None:
        src = "98412345678908"
        enc = fpe_encrypt_value(src, _KEY, _DIGITS, _TWEAK, checksum="gtin")
        assert fpe_decrypt_value(enc, _KEY, _DIGITS, _TWEAK, checksum="gtin") == src

    def test_npi_roundtrip(self) -> None:
        src = "1234567893"
        enc = fpe_encrypt_value(src, _KEY, _DIGITS, _TWEAK, checksum="npi")
        assert fpe_decrypt_value(enc, _KEY, _DIGITS, _TWEAK, checksum="npi") == src

    def test_isbn13_roundtrip(self) -> None:
        src = "9783161484100"
        enc = fpe_encrypt_value(src, _KEY, _DIGITS, _TWEAK, checksum="isbn13")
        assert fpe_decrypt_value(enc, _KEY, _DIGITS, _TWEAK, checksum="isbn13") == src

    def test_vin_roundtrip(self) -> None:
        src = "1HGCM82633A004352"
        enc = fpe_encrypt_value(src, _KEY, _VIN_ALPHA, _TWEAK, checksum="vin")
        assert fpe_decrypt_value(enc, _KEY, _VIN_ALPHA, _TWEAK, checksum="vin") == src


# ---------------------------------------------------------------------------
# Typed-error machine fields: scheme + code (not message prose)
# ---------------------------------------------------------------------------


class TestFpeChecksumErrorFields:
    """Every fail-closed raise must carry the machine-routable fields a caller
    keys on -- the ``scheme`` that failed and the stable ``code`` -- not just a
    human message. UI/manifest consumers route on these without parsing text.
    """

    _CODE = "fpe.checksum_unsupported"

    def test_unknown_scheme_error_fields(self) -> None:
        with pytest.raises(FpeChecksumError) as exc:
            fpe_encrypt_value("12345", _KEY, _DIGITS, _TWEAK, checksum="bogus")
        assert exc.value.scheme == "bogus"
        assert exc.value.code == self._CODE

    def test_iban_error_fields(self) -> None:
        with pytest.raises(FpeChecksumError) as exc:
            fpe_encrypt_value(
                "GB82WEST12345698765432",
                _KEY,
                "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                _TWEAK,
                checksum="iban",
            )
        assert exc.value.scheme == "iban"
        assert exc.value.code == self._CODE

    def test_exact_length_error_fields(self) -> None:
        # NPI has one exact length (10); a 3-char value violates it.
        with pytest.raises(FpeChecksumError) as exc:
            fpe_encrypt_value("123", _KEY, _DIGITS, _TWEAK, checksum="npi")
        assert exc.value.scheme == "npi"
        assert exc.value.code == self._CODE

    def test_min_length_error_fields(self) -> None:
        # Luhn is length-agnostic beyond a floor of 2 (body + check digit);
        # a single character trips the minimum-length raise.
        with pytest.raises(FpeChecksumError) as exc:
            fpe_encrypt_value("5", _KEY, _DIGITS, _TWEAK, checksum="luhn")
        assert exc.value.scheme == "luhn"
        assert exc.value.code == self._CODE


# ---------------------------------------------------------------------------
# Length-guard boundaries: exactly which lengths raise vs pass
# ---------------------------------------------------------------------------


class TestFpeChecksumLengthBoundary:
    """The fail-closed length guard is a boundary, not a one-sided floor.

    A value one below the minimum must raise; a value exactly at the minimum
    must pass. Widening the comparison (``<`` to ``<=``) would reject the
    shortest legal value, and dropping the guard entirely would let a
    below-minimum value slip through to a silent self-permutation.
    """

    def test_luhn_length_two_passes(self) -> None:
        # Exactly at the floor (1 body char + 1 check digit): must NOT raise.
        out = fpe_encrypt_value("18", _KEY, _DIGITS, _TWEAK, checksum="luhn")
        assert len(out) == 2
        assert checksums.validate("luhn", out)

    def test_vin_two_char_charset_used_verbatim(self) -> None:
        """A caller charset yielding exactly two VIN-legal characters is used as
        the permutation alphabet as-is -- it must not be treated as too small
        and collapsed to the digit fallback.

        The fallback fires only below two usable characters; at exactly two the
        two-symbol alphabet drives the permutation, which is observable as a
        different (still VIN-valid) output than the ten-digit fallback would
        produce. The output is pinned under the fixed test key/tweak.
        """
        # 17-char source whose 16 non-check chars are all within charset "01".
        src = "00000000011111111"
        out = fpe_encrypt_value(src, _KEY, "01", _TWEAK, checksum="vin")
        assert checksums.validate("vin", out)
        assert out == "11001011X10011010"

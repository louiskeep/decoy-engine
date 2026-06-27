"""Unit tests for decoy_engine.checksums (SP-04 / P5.INFRA.1).

Tests are written against the public API only:
    validate(scheme, value) -> bool
    calc_check_digit(scheme, body) -> str

TDD: these tests were written BEFORE the implementation so they drive the
Done-state spec (P5.INFRA.1: python-stdnum vendoring + checksum parameter).

Per-algorithm expected values are derived from authoritative sources:
  luhn   - python-stdnum 2.2 / Luhn (1954)
  npi    - CMS NPPES NPI check-digit spec (NPPES 2008, table in _npi.py)
  iban   - python-stdnum 2.2 + ISO 13616 / SWIFT registry
  vin    - NHTSA 49 CFR Part 565 / ISO 3779 test vectors
  isbn13 - python-stdnum 2.2 / ISO 2108
  ean13  - python-stdnum 2.2 / GS1 General Specifications
  gtin   - python-stdnum 2.2 / GS1 GTIN (14-digit variant)
"""

from __future__ import annotations

import pytest

import decoy_engine.checksums as checksums

# ---------------------------------------------------------------------------
# Luhn (credit-card mod-10)
# ---------------------------------------------------------------------------


class TestLuhn:
    """Luhn mod-10 check digit; standard credit-card PAN validation.

    Body = all digits except the trailing check digit.
    Reference values computed via stdnum.luhn (python-stdnum 2.2).
    """

    def test_calc_check_digit_known_good(self) -> None:
        # Visa test card 4532015112830366; body = first 15 digits
        assert checksums.calc_check_digit("luhn", "453201511283036") == "6"

    def test_calc_check_digit_second_card(self) -> None:
        # 4111111111111111 (classic test card); body = first 15 digits
        assert checksums.calc_check_digit("luhn", "411111111111111") == "1"

    def test_validate_valid(self) -> None:
        assert checksums.validate("luhn", "4532015112830366") is True

    def test_validate_invalid_check(self) -> None:
        # Flip the last digit
        assert checksums.validate("luhn", "4532015112830360") is False

    def test_validate_non_digits_rejected(self) -> None:
        assert checksums.validate("luhn", "not-a-number") is False


# ---------------------------------------------------------------------------
# NPI (US National Provider Identifier, CMS NPPES)
# ---------------------------------------------------------------------------


class TestNpi:
    """NPI check digit: Luhn applied to '80840' + 9-digit body.

    Reference: CMS NPPES NPI Check Digit Procedure
    https://www.cms.gov/Regulations-and-Guidance/Administrative-Simplification/
    NationalProvIdentStand/Downloads/NPIcheckdigit.pdf

    Verified against the examples in decoy_engine.storm._validators._npi_valid
    (independent implementation).
    """

    def test_calc_check_digit_cms_example_1(self) -> None:
        # CMS example: NPI 1234567893, body=123456789
        assert checksums.calc_check_digit("npi", "123456789") == "3"

    def test_calc_check_digit_cms_example_2(self) -> None:
        # From storm/_validators.py verified comment: 1679576722
        assert checksums.calc_check_digit("npi", "167957672") == "2"

    def test_calc_check_digit_cms_example_3(self) -> None:
        # From storm/_validators.py: 1000000004
        assert checksums.calc_check_digit("npi", "100000000") == "4"

    def test_validate_valid(self) -> None:
        assert checksums.validate("npi", "1234567893") is True

    def test_validate_invalid_check(self) -> None:
        assert checksums.validate("npi", "1234567890") is False

    def test_validate_wrong_length(self) -> None:
        assert checksums.validate("npi", "123456789") is False  # 9 digits, not 10

    def test_validate_non_digits_rejected(self) -> None:
        assert checksums.validate("npi", "12345678AB") is False

    def test_validate_npi_leading_digit_zero_invalid(self) -> None:
        # NPPES allocates 1 and 2 only; leading-0 is invalid even with correct check
        body = "023456789"
        check = checksums.calc_check_digit("npi", body)
        assert checksums.validate("npi", body + check) is False

    def test_validate_npi_leading_digit_nine_invalid(self) -> None:
        # Leading-9 is outside NPPES allocation
        body = "923456789"
        check = checksums.calc_check_digit("npi", body)
        assert checksums.validate("npi", body + check) is False

    def test_validate_npi_leading_digit_two_valid(self) -> None:
        # Leading-2 is a valid NPPES allocation; check digit = 2 for body 200000000
        assert checksums.validate("npi", "2000000002") is True


# ---------------------------------------------------------------------------
# IBAN (International Bank Account Number, ISO 13616 / mod-97)
# ---------------------------------------------------------------------------


class TestIban:
    """IBAN check digits at positions 2-3 (ISO 13616, mod-97).

    Body = CC (2 chars) + BBAN (variable-length alphanumeric).
    Check = 2-digit string inserted between CC and BBAN.
    Validation via python-stdnum 2.2 stdnum.iban.is_valid.

    Reference: ISO 13616, SWIFT IBAN Registry.
    """

    def test_calc_check_digit_gb(self) -> None:
        # GB82WEST12345698765432; CC=GB, BBAN=WEST12345698765432
        assert checksums.calc_check_digit("iban", "GBWEST12345698765432") == "82"

    def test_calc_check_digit_de(self) -> None:
        # DE89370400440532013000; CC=DE, BBAN=370400440532013000
        assert checksums.calc_check_digit("iban", "DE370400440532013000") == "89"

    def test_calc_check_digit_returns_two_chars(self) -> None:
        result = checksums.calc_check_digit("iban", "GBWEST12345698765432")
        assert len(result) == 2 and result.isdigit()

    def test_validate_valid_gb(self) -> None:
        assert checksums.validate("iban", "GB82WEST12345698765432") is True

    def test_validate_valid_de(self) -> None:
        assert checksums.validate("iban", "DE89370400440532013000") is True

    def test_validate_invalid_check(self) -> None:
        assert checksums.validate("iban", "GB00WEST12345698765432") is False

    def test_validate_garbage_rejected(self) -> None:
        assert checksums.validate("iban", "NOTANIBAN") is False


# ---------------------------------------------------------------------------
# VIN (Vehicle Identification Number, ISO 3779 / NHTSA 49 CFR Part 565)
# ---------------------------------------------------------------------------


class TestVin:
    """VIN check digit at position 9 (1-indexed) = index 8 (0-indexed).

    Body = the 16 non-check characters (positions 0-7 and 9-16 of the
    full 17-character VIN).
    Check = 1 character: '0'-'9' or 'X' (represents 10).

    Algorithm: sum of transliterated character values times positional
    weights, mod 11.  Position 9 has weight 0 so it does not contribute
    to the sum -- the body can be computed without a placeholder.

    Reference: ISO 3779, NHTSA 49 CFR Part 565 (Vehicle Identification
    Number Requirements, 2008).
    """

    def test_calc_check_digit_honda_accord(self) -> None:
        # VIN 1HGCM82633A004352: body = positions 0-7 + 9-16
        # = '1HGCM826' + '3A004352' = '1HGCM8263A004352'
        assert checksums.calc_check_digit("vin", "1HGCM8263A004352") == "3"

    def test_calc_check_digit_second_vin(self) -> None:
        # VIN 1FTZX1722XKB43466: check digit at position 9 is '2'
        # body = '1FTZX172' + 'XKB43466' = '1FTZX172XKB43466'
        body = "1FTZX172" + "XKB43466"
        result = checksums.calc_check_digit("vin", body)
        # Reconstruct and validate
        full_vin = body[:8] + result + body[8:]
        assert checksums.validate("vin", full_vin) is True

    def test_validate_valid(self) -> None:
        assert checksums.validate("vin", "1HGCM82633A004352") is True

    def test_validate_invalid_check(self) -> None:
        # Correct VIN with wrong check digit at position 8
        assert checksums.validate("vin", "1HGCM82693A004352") is False

    def test_validate_wrong_length(self) -> None:
        assert checksums.validate("vin", "1HGCM826") is False  # too short

    def test_validate_forbidden_character(self) -> None:
        # I, O, Q are not used in VIN
        assert checksums.validate("vin", "1HGCI82633A004352") is False


# ---------------------------------------------------------------------------
# EAN-13 (International Article Number)
# ---------------------------------------------------------------------------


class TestEan13:
    """EAN-13 check digit: GS1 mod-10 alternating-weight algorithm.

    Body = first 12 digits; check = 1 trailing digit.
    Validation and check-digit computation via python-stdnum 2.2 stdnum.ean.
    """

    def test_calc_check_digit_known_good(self) -> None:
        # EAN-13 4006381333931; body = 400638133393
        assert checksums.calc_check_digit("ean13", "400638133393") == "1"

    def test_calc_check_digit_second_barcode(self) -> None:
        # EAN-13 9780201379624; body = 978020137962
        assert checksums.calc_check_digit("ean13", "978020137962") == "4"

    def test_validate_valid(self) -> None:
        assert checksums.validate("ean13", "4006381333931") is True

    def test_validate_invalid_check(self) -> None:
        assert checksums.validate("ean13", "4006381333930") is False

    def test_validate_non_digits_rejected(self) -> None:
        assert checksums.validate("ean13", "400638133393X") is False


# ---------------------------------------------------------------------------
# ISBN-13 (International Standard Book Number, 13-digit form)
# ---------------------------------------------------------------------------


class TestIsbn13:
    """ISBN-13 check digit: same GS1 algorithm as EAN-13.

    Body = first 12 digits (must begin with 978 or 979 for valid ISBN).
    Validation via python-stdnum 2.2 stdnum.isbn.is_valid.
    """

    def test_calc_check_digit_known_good(self) -> None:
        # ISBN-13 9783161484100; body = 978316148410
        assert checksums.calc_check_digit("isbn13", "978316148410") == "0"

    def test_calc_check_digit_second_isbn(self) -> None:
        # ISBN-13 9780471117094; body = 978047111709
        assert checksums.calc_check_digit("isbn13", "978047111709") == "4"

    def test_validate_valid(self) -> None:
        assert checksums.validate("isbn13", "9783161484100") is True

    def test_validate_invalid_check(self) -> None:
        assert checksums.validate("isbn13", "9783161484101") is False

    def test_validate_non_digits_rejected(self) -> None:
        assert checksums.validate("isbn13", "978316148410X") is False


# ---------------------------------------------------------------------------
# GTIN (Global Trade Item Number, 14-digit variant)
# ---------------------------------------------------------------------------


class TestGtin:
    """GTIN-14 check digit: same GS1 algorithm as EAN-13, applied to 13 digits.

    python-stdnum's stdnum.ean handles 8, 12, 13, and 14-digit GS1 barcodes.
    Body = first N-1 digits; check = 1 trailing digit.
    """

    def test_calc_check_digit_gtin14(self) -> None:
        # GTIN-14 98412345678908; body = 9841234567890 (13 digits)
        assert checksums.calc_check_digit("gtin", "9841234567890") == "8"

    def test_calc_check_digit_gtin13(self) -> None:
        # 13-digit GTIN (EAN-13 compatible); body = 400638133393
        assert checksums.calc_check_digit("gtin", "400638133393") == "1"

    def test_validate_valid_gtin14(self) -> None:
        assert checksums.validate("gtin", "98412345678908") is True

    def test_validate_invalid_check(self) -> None:
        assert checksums.validate("gtin", "98412345678901") is False

    def test_validate_also_accepts_ean13(self) -> None:
        # stdnum.ean is the backing validator and accepts 13-digit GTINs
        assert checksums.validate("gtin", "4006381333931") is True


# ---------------------------------------------------------------------------
# Dispatch: unknown scheme
# ---------------------------------------------------------------------------


class TestUnknownScheme:
    def test_validate_unknown_scheme_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown checksum scheme"):
            checksums.validate("bogus_scheme", "12345")

    def test_calc_check_digit_unknown_scheme_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown checksum scheme"):
            checksums.calc_check_digit("bogus_scheme", "12345")


# ---------------------------------------------------------------------------
# FPE + checksum integration
# ---------------------------------------------------------------------------


class TestFpeChecksumIntegration:
    """FPE strategy with checksum: param (P5.INFRA.1 Done-state test).

    After FPE-masking an NPI, the output must pass NPI validation.
    Uses the V2 FpeStrategyHandler directly to mirror the production path.
    """

    def _make_ctx(self) -> object:
        from decoy_engine.execution._adapter import StrategyContext
        from decoy_engine.generation.pool._cache import PoolCache
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        return StrategyContext(
            registry=get_default_registry(),
            pool_cache=PoolCache(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
            job_seed=(0xDECAFBAD).to_bytes(8, "big"),
        )

    def _npi_col(self) -> object:
        from decoy_engine.plan._types import ColumnSeed

        return ColumnSeed(
            namespace="npi_ns",
            strategy="fpe",
            provider="fpe",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=True,
            provider_config=(("charset", "digits"), ("checksum", "npi")),
            coherent_with=(),
        )

    def test_fpe_npi_checksum_output_is_npi_valid(self) -> None:
        """Mask an NPI with checksum:npi; the output must pass NPI validation."""
        import pandas as pd

        from decoy_engine.execution._strategies._fpe import FpeStrategyHandler

        npis = ["1234567893", "1679576722", "1000000004"]
        df = pd.DataFrame({"npi": npis})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "npi", self._npi_col(), self._make_ctx())
        for val in out["npi"].tolist():
            assert checksums.validate("npi", val), f"FPE output {val!r} is not a valid NPI"

    def test_fpe_npi_checksum_deterministic(self) -> None:
        """Same NPI + key + checksum -> same masked output across two runs."""
        import pandas as pd

        from decoy_engine.execution._strategies._fpe import FpeStrategyHandler

        npi = "1234567893"
        df_a = pd.DataFrame({"npi": [npi]})
        df_b = pd.DataFrame({"npi": [npi]})
        out_a, _ = FpeStrategyHandler(chunk_count=1).run(
            df_a, "npi", self._npi_col(), self._make_ctx()
        )
        out_b, _ = FpeStrategyHandler(chunk_count=1).run(
            df_b, "npi", self._npi_col(), self._make_ctx()
        )
        assert out_a["npi"].tolist()[0] == out_b["npi"].tolist()[0]

    def test_fpe_npi_output_differs_from_input(self) -> None:
        """FPE with checksum must still be a non-trivial transformation."""
        import pandas as pd

        from decoy_engine.execution._strategies._fpe import FpeStrategyHandler

        # Use many NPIs and verify at least some are changed (vanishingly unlikely
        # that all 3 are fixed points under the test key)
        npis = ["1234567893", "1679576722", "1000000004"]
        df = pd.DataFrame({"npi": npis})
        out, _ = FpeStrategyHandler(chunk_count=1).run(df, "npi", self._npi_col(), self._make_ctx())
        masked = out["npi"].tolist()
        assert masked != npis, "FPE with checksum produced all fixed points (key/impl bug)"


# ---------------------------------------------------------------------------
# FPE + checksum: fpe_encrypt_value / fpe_decrypt_value public functions
# ---------------------------------------------------------------------------


class TestFpeEncryptDecryptChecksum:
    """Unit-level coverage for fpe_encrypt_value / fpe_decrypt_value with checksum.

    These test the transforms.fpe module functions directly (the V1 path
    that the V2 strategy delegates to).
    """

    _KEY = bytes(range(32))
    _TWEAK = b"npi_col"
    _CHARSET = "0123456789"

    def test_encrypt_npi_output_is_npi_valid(self) -> None:
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        enc = fpe_encrypt_value("1234567893", self._KEY, self._CHARSET, self._TWEAK, checksum="npi")
        assert len(enc) == 10 and enc.isdigit()
        assert checksums.validate("npi", enc)

    def test_encrypt_luhn_output_is_luhn_valid(self) -> None:
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        pan = "4532015112830366"
        enc = fpe_encrypt_value(pan, self._KEY, self._CHARSET, self._TWEAK, checksum="luhn")
        assert len(enc) == len(pan)
        assert checksums.validate("luhn", enc)

    def test_encrypt_ean13_output_is_ean_valid(self) -> None:
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        enc = fpe_encrypt_value(
            "4006381333931", self._KEY, self._CHARSET, self._TWEAK, checksum="ean13"
        )
        assert len(enc) == 13
        assert checksums.validate("ean13", enc)

    def test_checksum_deterministic(self) -> None:
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        a = fpe_encrypt_value("1234567893", self._KEY, self._CHARSET, self._TWEAK, checksum="npi")
        b = fpe_encrypt_value("1234567893", self._KEY, self._CHARSET, self._TWEAK, checksum="npi")
        assert a == b

    def test_checksum_and_validate_luhn_coexist_checksum_wins(self) -> None:
        """If both checksum and validate_luhn are set, checksum takes priority."""
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        enc = fpe_encrypt_value(
            "1234567893",
            self._KEY,
            self._CHARSET,
            self._TWEAK,
            validate_luhn=True,
            checksum="npi",
        )
        # Result should be NPI-valid (checksum takes priority)
        assert checksums.validate("npi", enc)

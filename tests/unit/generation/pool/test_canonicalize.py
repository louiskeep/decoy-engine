"""_canonicalize_source per-dtype envelope tests (S5 spec §5.1).

The per-dtype rules are part of the determinism envelope per R3. Any
change requires a SEED_PROTOCOL_VERSION bump. These tests pin the
encoding for every supported dtype family and the hard-error dtypes.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._errors import GenerationError


class TestStringNFC:
    def test_ascii_string(self) -> None:
        assert _canonicalize_source("hello") == b"hello"

    def test_nfc_normalization_unifies_decomposed_forms(self) -> None:
        """NFC vs NFD: same character represented as composed or decomposed
        must canonicalize identically. e-with-acute can be U+00E9 (NFC) or
        e + U+0301 (NFD); the helper normalizes to NFC."""
        nfc = "é"
        nfd = "é"
        assert _canonicalize_source(nfc) == _canonicalize_source(nfd)

    def test_non_string_object_falls_back_to_str(self) -> None:
        class Obj:
            def __str__(self) -> str:
                return "obj"

        assert _canonicalize_source(Obj()) == b"obj"


class TestInteger:
    def test_zero_big_endian_8_bytes(self) -> None:
        assert _canonicalize_source(0) == b"\x00\x00\x00\x00\x00\x00\x00\x00"

    def test_one_big_endian(self) -> None:
        assert _canonicalize_source(1) == b"\x00\x00\x00\x00\x00\x00\x00\x01"

    def test_negative_one_twos_complement(self) -> None:
        assert _canonicalize_source(-1) == b"\xff\xff\xff\xff\xff\xff\xff\xff"

    def test_large_int_fits(self) -> None:
        # 2**63 - 1 = max int64 signed
        assert _canonicalize_source(2**63 - 1) == b"\x7f\xff\xff\xff\xff\xff\xff\xff"


class TestBoolean:
    def test_true(self) -> None:
        assert _canonicalize_source(True) == b"\x01"

    def test_false(self) -> None:
        assert _canonicalize_source(False) == b"\x00"


class TestDatetime:
    def test_utc_datetime(self) -> None:
        dt = datetime(2026, 5, 27, 14, 0, 0, tzinfo=timezone.utc)
        out = _canonicalize_source(dt)
        assert out.startswith(b"2026-05-27")
        assert b"+00:00" in out or b"Z" in out

    def test_timezone_naive_raises(self) -> None:
        dt = datetime(2026, 5, 27, 14, 0, 0)  # no tz
        with pytest.raises(GenerationError) as excinfo:
            _canonicalize_source(dt)
        assert excinfo.value.code == "timezone_naive_datetime"

    def test_date_iso_form(self) -> None:
        assert _canonicalize_source(date(2026, 5, 27)) == b"2026-05-27"


class TestFloatHardError:
    """Per S5 PO PQ-call: floats raise hard rather than commit to an
    IEEE-754 encoding (which would lock into the determinism envelope)."""

    def test_float_raises(self) -> None:
        with pytest.raises(GenerationError) as excinfo:
            _canonicalize_source(0.1)
        assert excinfo.value.code == "float_canonicalization_unsupported"

    def test_negative_float_raises(self) -> None:
        with pytest.raises(GenerationError) as excinfo:
            _canonicalize_source(-1.5)
        assert excinfo.value.code == "float_canonicalization_unsupported"


class TestDecimal:
    def test_decimal_uses_canonical_string(self) -> None:
        assert _canonicalize_source(Decimal("1.23")) == b"1.23"

    def test_decimal_no_trailing_zeros_via_default_str(self) -> None:
        """Note: Decimal.str preserves trailing zeros per its own contract
        (e.g. Decimal('1.20').__str__() == '1.20'). We do not strip; the
        caller is responsible for canonicalizing Decimals before passing
        in if they need normalized form."""
        assert _canonicalize_source(Decimal("1.20")) == b"1.20"


class TestNullDefensiveGuard:
    """Per S5 spec §5.1: nulls preserve at the mask layer; reaching
    canonicalize indicates an upstream bug."""

    def test_none_raises(self) -> None:
        with pytest.raises(GenerationError) as excinfo:
            _canonicalize_source(None)
        assert excinfo.value.code == "null_canonicalization_unreachable"

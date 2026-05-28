"""_canonicalize_source per-dtype envelope tests (S5 spec §5.1).

The per-dtype rules are part of the determinism envelope per R3. Any
change requires a SEED_PROTOCOL_VERSION bump. These tests pin the
encoding for every supported dtype family and the hard-error dtypes.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
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
    """v2 envelope (F-series NF1/NF2): length-prefixed minimal-width two's
    complement big-endian (4-byte length prefix + minimal body). Replaces the
    fixed 8-byte form, which overflowed for |value| >= 2**63 and missed numpy
    integer scalars."""

    def test_zero(self) -> None:
        # length 1, body 0x00
        assert _canonicalize_source(0) == b"\x00\x00\x00\x01\x00"

    def test_one(self) -> None:
        assert _canonicalize_source(1) == b"\x00\x00\x00\x01\x01"

    def test_negative_one_twos_complement(self) -> None:
        assert _canonicalize_source(-1) == b"\x00\x00\x00\x01\xff"

    def test_max_int64_uses_eight_body_bytes(self) -> None:
        # 2**63 - 1 = max signed int64: 8 body bytes, no overflow.
        assert (
            _canonicalize_source(2**63 - 1) == b"\x00\x00\x00\x08\x7f\xff\xff\xff\xff\xff\xff\xff"
        )

    def test_beyond_int64_does_not_overflow(self) -> None:
        """NF2: the prior fixed 8-byte encoding raised OverflowError here; the
        variable-length form widens the body instead."""
        # 2**63 needs 9 body bytes (sign bit forces the extra leading 0x00).
        assert _canonicalize_source(2**63) == bytes.fromhex("00000009008000000000000000")
        # 2**64 likewise; arbitrary magnitude is supported.
        assert _canonicalize_source(2**64) == bytes.fromhex("00000009010000000000000000")

    def test_injective_across_magnitudes(self) -> None:
        seen = {
            _canonicalize_source(n) for n in (-2, -1, 0, 1, 2, 127, 128, 255, 256, 2**63, 2**70)
        }
        assert len(seen) == 11  # all distinct

    def test_numpy_int_matches_python_int(self) -> None:
        """NF1: pd.Series.iloc[i] returns numpy scalars. They must canonicalize
        identically to the equivalent Python int (the prior `isinstance(int)`
        check missed numpy ints, silently routing them through the str
        fallback so 42 and numpy.int64(42) produced different bytes)."""
        for n in (0, 1, -1, 42, 2**31 - 1):
            assert _canonicalize_source(np.int64(n)) == _canonicalize_source(int(n))
        # Unsigned numpy ints too (e.g. uint64 indices).
        assert _canonicalize_source(np.uint64(42)) == _canonicalize_source(42)


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

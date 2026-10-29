"""F06 A2-A7: strict parse/format kernels (RED until Slice 1)."""

from __future__ import annotations

import decimal
import threading
from datetime import date
from decimal import Decimal

import pytest

from tests.acceptance._f06_support import DEC_5_2, EUR, INT64, ISO_DATE, USD, zero_pad


def _api():
    from decoy_engine.declared_casting import format_value, parse_value

    return parse_value, format_value


def _code(outcome) -> str:
    assert not outcome.ok
    return str(outcome.reason)


# -- A2 / A9-adjacent: no truncation ---------------------------------------------------------


def test_a2_zero_pad_formats_and_never_truncates() -> None:
    parse, fmt = _api()
    assert fmt(zero_pad(3), 42).value == "042"
    over = fmt(zero_pad(3), 1000)
    assert not over.ok and _code(over) == "declared_output_width_overflow"
    # the sign consumes width
    assert fmt(zero_pad(3), -42).value == "-42"
    assert fmt(zero_pad(4), -42).value == "-042"
    assert _code(fmt(zero_pad(3), -420)) == "declared_output_width_overflow"
    assert parse(INT64, "042").value == 42


@pytest.mark.parametrize(
    ("spec", "value"),
    [
        ({**DEC_5_2, "width": 5, "width_unit": "code_points", "pad": " ", "align": "right"}, Decimal("-99.99")),
        ({**USD, "width": 6, "width_unit": "code_points", "pad": " ", "align": "right"}, Decimal("123.50")),
        ({**EUR, "width": 6, "width_unit": "code_points", "pad": " ", "align": "right"}, Decimal("1.50")),
        (
            {"id": "decimal/grouped/v1", "precision": 8, "scale": 2, "radix": ".", "group": ",",
             "width": 6, "width_unit": "code_points", "pad": " ", "align": "right"},
            Decimal("1234.50"),
        ),
    ],
)
def test_a2_width_includes_sign_token_grouping(spec, value) -> None:
    _, fmt = _api()
    assert _code(fmt(spec, value)) == "declared_output_width_overflow"


@pytest.mark.parametrize("bit_width", [8, 16, 32, 64])
@pytest.mark.parametrize("signed", [True, False])
def test_integer_bit_width_boundaries(bit_width: int, signed: bool) -> None:
    parse, _ = _api()
    spec = {"id": "integer/ascii/v1", "signed": signed, "bit_width": bit_width}
    hi = 2 ** (bit_width - 1) - 1 if signed else 2**bit_width - 1
    lo = -(2 ** (bit_width - 1)) if signed else 0
    for v in (0, 1, hi, lo):
        assert parse(spec, str(v)).value == v
    assert _code(parse(spec, str(hi + 1))) == "declared_integer_overflow"
    assert _code(parse(spec, str(lo - 1))) == "declared_integer_overflow"
    if signed:
        assert parse(spec, "-1").value == -1
    else:
        assert _code(parse(spec, "-1")) == "declared_integer_overflow"


@pytest.mark.parametrize("bad", ["", " 1", "1 ", "1_0", "1e3", "０１", "--1", "+", "0x10", "1.0"])
def test_integer_grammar_is_strict(bad: str) -> None:
    parse, _ = _api()
    assert _code(parse(INT64, bad)) == "declared_integer_invalid"


# -- A3 decimal ------------------------------------------------------------------------------


def test_a3_decimal_precision_and_scale() -> None:
    parse, fmt = _api()
    assert parse(DEC_5_2, "999.99").value == Decimal("999.99")
    assert _code(parse(DEC_5_2, "1000.00")) == "declared_decimal_overflow"
    assert _code(parse(DEC_5_2, "1.234")) == "declared_decimal_precision_loss"
    assert parse(DEC_5_2, "0.1").value == Decimal("0.10")
    assert str(parse(DEC_5_2, "0.1").value) == "0.10"
    assert _code(parse(DEC_5_2, 0.1)) == "declared_decimal_invalid"  # a float is refused
    assert fmt(DEC_5_2, Decimal("999.99")).value == "999.99"
    assert _code(fmt(DEC_5_2, 0.5)) == "declared_decimal_invalid"


def test_a3_signed_zero_is_retained() -> None:
    parse, fmt = _api()
    v = parse(DEC_5_2, "-0.00").value
    assert v.is_signed() and v == 0
    assert fmt(DEC_5_2, v).value == "-0.00"


def test_a3_hostile_decimal_context_cannot_change_results() -> None:
    parse, fmt = _api()
    baseline = (parse(DEC_5_2, "999.99").value, fmt(DEC_5_2, Decimal("123.40")).value)
    hostile = decimal.Context(prec=2, rounding=decimal.ROUND_DOWN, Emin=-3, Emax=3, traps=[])
    token = decimal.getcontext().copy()
    decimal.setcontext(hostile)
    try:
        got = (parse(DEC_5_2, "999.99").value, fmt(DEC_5_2, Decimal("123.40")).value)
        precision_loss = parse(DEC_5_2, "1.234")
    finally:
        decimal.setcontext(token)
    assert got == baseline
    assert str(got[0]) == "999.99"
    assert _code(precision_loss) == "declared_decimal_precision_loss"

    seen: list[object] = []

    def worker() -> None:
        decimal.setcontext(hostile)
        seen.append(parse(DEC_5_2, "999.99").value)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen == [Decimal("999.99")]


def test_a3_precision_is_bounded_1_to_38() -> None:
    from decoy_engine.declared_casting import validate_format_spec

    validate_format_spec({"id": "decimal/plain/v1", "precision": 38, "scale": 2})
    for p in (0, 39, 10**9, "5", 5.0, True):
        with pytest.raises(Exception) as exc:
            validate_format_spec({"id": "decimal/plain/v1", "precision": p, "scale": 0})
        assert getattr(exc.value, "code", None) == "declared_cast_precision_invalid"
    for s in (-1, 6):
        with pytest.raises(Exception) as exc:
            validate_format_spec({"id": "decimal/plain/v1", "precision": 5, "scale": s})
        assert getattr(exc.value, "code", None) == "declared_cast_scale_invalid"


# -- A4 / A5 dates ---------------------------------------------------------------------------

STRPTIME_MDY = {"id": "date/strptime/v1", "format": "%m/%d/%Y"}
STRFTIME_MDY = {"id": "date/strftime/v1", "format": "%m/%d/%Y"}


def test_a4_dates_are_explicit() -> None:
    parse, fmt = _api()
    assert parse(ISO_DATE, "2026-02-01").value == date(2026, 2, 1)
    for bad in ("2026-2-1", "20260201", "2026-02-01 ", "2026-02-30", "2026-02-01T00:00", " 2026-02-01", "２０２６-02-01"):
        assert _code(parse(ISO_DATE, bad)) == "declared_date_invalid", bad
    assert parse(STRPTIME_MDY, "01/02/2026").value == date(2026, 1, 2)
    assert fmt(STRFTIME_MDY, date(2026, 1, 2)).value == "01/02/2026"
    assert _code(parse(STRPTIME_MDY, "1/2/2026")) == "declared_date_invalid"
    assert _code(parse(STRPTIME_MDY, "01/02/2026x")) == "declared_date_invalid"
    assert fmt(ISO_DATE, date(1, 1, 1)).value == "0001-01-01"


@pytest.mark.parametrize("directive", ["%b", "%B", "%c", "%x", "%X", "%Z", "%y", "%j", "%-d", "%a", "%p"])
def test_a4_locale_and_platform_directives_are_rejected(directive: str) -> None:
    from decoy_engine.declared_casting import validate_format_spec

    with pytest.raises(Exception) as exc:
        validate_format_spec({"id": "date/strptime/v1", "format": f"%Y {directive} %d"})
    assert getattr(exc.value, "code", None) == "declared_cast_directive_unsupported"


def test_a4_requires_all_three_fields_and_separators() -> None:
    from decoy_engine.declared_casting import validate_format_spec

    for fmt in ("%Y-%m", "%Y%m%d", "%d/%m"):
        with pytest.raises(Exception) as exc:
            validate_format_spec({"id": "date/strptime/v1", "format": fmt})
        assert getattr(exc.value, "code", None) == "declared_cast_directive_unsupported"


def test_a5_locale_independence(monkeypatch: pytest.MonkeyPatch) -> None:
    import calendar
    import locale

    parse, fmt = _api()
    baseline = (
        parse(DEC_5_2, "999.99").value,
        fmt(DEC_5_2, Decimal("1.50")).value,
        parse(STRPTIME_MDY, "01/02/2026").value,
        fmt(STRFTIME_MDY, date(2026, 1, 2)).value,
        parse(USD, "$1,234.50").value,
    )
    monkeypatch.setattr(
        locale,
        "localeconv",
        lambda: {"decimal_point": ",", "thousands_sep": ".", "grouping": [2, 0], "currency_symbol": "¥"},
    )
    monkeypatch.setattr(calendar, "month_name", ["", *("X" * i for i in range(1, 13))])
    monkeypatch.setattr(calendar, "day_name", ["Z"] * 7)
    again = (
        parse(DEC_5_2, "999.99").value,
        fmt(DEC_5_2, Decimal("1.50")).value,
        parse(STRPTIME_MDY, "01/02/2026").value,
        fmt(STRFTIME_MDY, date(2026, 1, 2)).value,
        parse(USD, "$1,234.50").value,
    )
    assert again == baseline


# -- A6 currency -----------------------------------------------------------------------------


def test_a6_currency() -> None:
    parse, fmt = _api()
    assert parse(USD, "$1,234.50").value == Decimal("1234.50")
    assert parse(EUR, "1.234,50 EUR").value == Decimal("1234.50")
    assert parse(USD, "-$1,234.50").value == Decimal("-1234.50")
    for bad in ("12,34.56", "1,234.50$", "(1,234.50)", "1,234.50", "$1,23.50", "$,234.50", "$1234,567.00"):
        assert _code(parse(USD, bad)) == "declared_currency_invalid", bad
    assert _code(parse(USD, "$1234.50")) == "declared_currency_invalid"  # grouping is canonical
    assert fmt(USD, Decimal("1234.5")).value == "$1,234.50"
    assert _code(fmt(USD, Decimal("1234.567"))) == "declared_decimal_precision_loss"
    assert fmt(USD, Decimal("1234.50")).value == "$1,234.50"
    assert fmt(EUR, Decimal("-1234.50")).value == "-1.234,50 EUR"
    accounting = {**USD, "sign_style": "accounting_parentheses"}
    assert parse(accounting, "($1,234.50)").value == Decimal("-1234.50")
    assert _code(parse(accounting, "-$1,234.50")) == "declared_currency_invalid"
    assert fmt(accounting, Decimal("-1234.50")).value == "($1,234.50)"


def test_a6_currency_code_is_mandatory_and_validated() -> None:
    from decoy_engine.declared_casting import validate_format_spec

    missing = {k: v for k, v in USD.items() if k != "currency_code"}
    for bad in (missing, {**USD, "currency_code": "usd"}, {**USD, "currency_code": "US"}, {**USD, "currency_code": "US1"}):
        with pytest.raises(Exception) as exc:
            validate_format_spec(bad)
        assert getattr(exc.value, "code", None) == "declared_cast_currency_code_invalid"
    with pytest.raises(Exception) as exc:
        validate_format_spec({**USD, "token": "€" * 40})  # > 32 UTF-8 bytes
    assert getattr(exc.value, "code", None) == "declared_cast_contract_invalid"


def test_b5_unicode_currency_width_is_unit_aware() -> None:
    from decoy_engine.declared_casting import output_width

    euro = {**EUR, "token": "€", "token_position": "prefix", "radix": ".", "group": ","}
    base = {**euro, "width": 8, "pad": " ", "align": "right"}
    assert output_width({**base, "width_unit": "code_points"}) == 8
    # `€` is 3 UTF-8 bytes; a byte-width contract measures it as such, so the same declared
    # width holds fewer code points and the statically derived worst case differs per unit.
    from decoy_engine.declared_casting import max_rendered_width

    assert max_rendered_width({**euro, "width": 20, "width_unit": "bytes", "pad": " ", "align": "right"}) > max_rendered_width(
        {**euro, "width": 20, "width_unit": "code_points", "pad": " ", "align": "right"}
    )


# -- A7 COBOL --------------------------------------------------------------------------------

OVERPUNCH = {"id": "cobol/overpunch-ascii-trailing/v1", "precision": 5, "scale": 2}
ZONED = {"id": "cobol/zoned-ebcdic-trailing/v1", "precision": 3, "scale": 0, "signed": True}
ZONED_U = {**ZONED, "signed": False}
COMP3_ODD = {"id": "cobol/comp-3/v1", "precision": 5, "scale": 0, "signed": True}
COMP3_EVEN = {"id": "cobol/comp-3/v1", "precision": 4, "scale": 2, "signed": True}
COMP3_U = {"id": "cobol/comp-3/v1", "precision": 3, "scale": 0, "signed": False}
POS = "{ABCDEFGHI"
NEG = "}JKLMNOPQR"


@pytest.mark.parametrize("digit", range(10))
def test_a7_overpunch_every_terminal(digit: int) -> None:
    parse, fmt = _api()
    pos = parse(OVERPUNCH, f"0012{POS[digit]}")
    neg = parse(OVERPUNCH, f"0012{NEG[digit]}")
    assert pos.value == Decimal(f"{120 + digit}").scaleb(-2)
    assert neg.value == -Decimal(f"{120 + digit}").scaleb(-2)
    assert fmt(OVERPUNCH, pos.value).value == f"0012{POS[digit]}"
    assert fmt(OVERPUNCH, neg.value).value == f"0012{NEG[digit]}"


def test_a7_overpunch_rejections_and_signed_zero() -> None:
    parse, fmt = _api()
    assert parse(OVERPUNCH, "0000}").value.is_signed()
    assert fmt(OVERPUNCH, parse(OVERPUNCH, "0000}").value).value == "0000}"
    for bad in ("00123", "0012", "001234{", "00 2{", "0012a", "0012{ ", "12ZZ{"):
        assert _code(parse(OVERPUNCH, bad)) == "declared_cobol_invalid", bad
    assert _code(fmt(OVERPUNCH, Decimal("1234.56"))) == "declared_decimal_overflow"


def test_a7_zoned_ebcdic() -> None:
    parse, fmt = _api()
    assert parse(ZONED, bytes([0xF1, 0xF2, 0xC3])).value == 123
    assert parse(ZONED, bytes([0xF1, 0xF2, 0xA3])).value == 123
    assert parse(ZONED, bytes([0xF1, 0xF2, 0xE3])).value == 123
    assert parse(ZONED, bytes([0xF1, 0xF2, 0xD3])).value == -123
    assert parse(ZONED, bytes([0xF1, 0xF2, 0xB3])).value == -123
    assert parse(ZONED_U, bytes([0xF1, 0xF2, 0xF3])).value == 123
    assert parse(ZONED, bytes([0xF0, 0xF0, 0xD0])).value.is_signed()
    for bad in (
        bytes([0xF1, 0xF2, 0xF3]),  # F is unsigned-only
        bytes([0xF1, 0xFA, 0xC3]),  # digit nibble A
        bytes([0xF1, 0xC2, 0xC3]),  # sign zone in a non-final position
        bytes([0xF1, 0xC3]),  # short
        bytes([0xF1, 0xF2, 0xC3, 0xC4]),  # long
        bytes([0xF1, 0xF2, 0x03]),  # zone 0
    ):
        assert _code(parse(ZONED, bad)) == "declared_cobol_invalid", bad
    assert _code(parse(ZONED_U, bytes([0xF1, 0xF2, 0xC3]))) == "declared_cobol_invalid"
    assert fmt(ZONED, Decimal(123)).value == bytes([0xF1, 0xF2, 0xC3])
    assert fmt(ZONED, Decimal(-123)).value == bytes([0xF1, 0xF2, 0xD3])
    assert fmt(ZONED_U, Decimal(123)).value == bytes([0xF1, 0xF2, 0xF3])
    assert _code(fmt(ZONED_U, Decimal(-1))) == "declared_cobol_invalid"
    assert _code(parse(ZONED, "text")) == "declared_cobol_invalid"  # binary kind refuses str


def test_a7_comp3() -> None:
    parse, fmt = _api()
    assert parse(COMP3_ODD, bytes([0x12, 0x34, 0x5C])).value == 12345
    assert parse(COMP3_ODD, bytes([0x12, 0x34, 0x5D])).value == -12345
    assert parse(COMP3_ODD, bytes([0x00, 0x00, 0x0D])).value.is_signed()
    assert parse(COMP3_EVEN, bytes([0x01, 0x23, 0x4C])).value == Decimal("12.34")
    assert parse(COMP3_U, bytes([0x12, 0x3F])).value == 123
    for bad in (
        bytes([0x12, 0x34, 0x5A]),  # A is not an admitted sign
        bytes([0x12, 0x34, 0x5F]),  # F needs signed: false
        bytes([0x1A, 0x34, 0x5C]),  # digit nibble A
        bytes([0x12, 0x34]),  # short
        bytes([0x12, 0x34, 0x5C, 0x00]),  # long
    ):
        assert _code(parse(COMP3_ODD, bad)) == "declared_cobol_invalid", bad
    assert _code(parse(COMP3_EVEN, bytes([0x11, 0x23, 0x4C]))) == "declared_cobol_invalid"  # unused nibble
    assert _code(parse(COMP3_U, bytes([0x12, 0x3C]))) == "declared_cobol_invalid"
    assert fmt(COMP3_ODD, Decimal(12345)).value == bytes([0x12, 0x34, 0x5C])
    assert fmt(COMP3_ODD, Decimal(-12345)).value == bytes([0x12, 0x34, 0x5D])
    assert fmt(COMP3_EVEN, Decimal("12.34")).value == bytes([0x01, 0x23, 0x4C])
    assert fmt(COMP3_U, Decimal(123)).value == bytes([0x12, 0x3F])
    assert _code(fmt(COMP3_ODD, Decimal(123456))) == "declared_decimal_overflow"


def test_a7_scale_is_applied_with_scaleb_not_ambient_context() -> None:
    parse, _ = _api()
    spec = {"id": "cobol/comp-3/v1", "precision": 20, "scale": 18, "signed": True}
    raw = bytes.fromhex("0" + "12345678901234567890" + "c")
    assert len(raw) == 11
    hostile = decimal.Context(prec=3, rounding=decimal.ROUND_HALF_UP, traps=[])
    old = decimal.getcontext().copy()
    decimal.setcontext(hostile)
    try:
        got = parse(spec, raw)
    finally:
        decimal.setcontext(old)
    assert got.ok
    assert got.value == Decimal("12.345678901234567890")
    assert str(got.value) == "12.345678901234567890"


# -- sentinel --------------------------------------------------------------------------------


def test_a8_sentinel_exact_matching_text() -> None:
    from decoy_engine.declared_casting import match_sentinel

    spec = {"id": "sentinel/exact/v1", "values": ["TBD", ""]}
    assert match_sentinel(spec, "TBD", binary=False)
    assert match_sentinel(spec, "", binary=False)
    assert not match_sentinel(spec, "tbd", binary=False)
    assert not match_sentinel(spec, " TBD", binary=False)
    assert not match_sentinel(spec, None, binary=False)
    only_tbd = {"id": "sentinel/exact/v1", "values": ["TBD"]}
    assert not match_sentinel(only_tbd, "", binary=False)


def test_sentinel_binary_is_exact_byte_match_without_pad_removal() -> None:
    from decoy_engine.declared_casting import match_sentinel

    spec = {"id": "sentinel/exact/v1", "values": ["ffffff"]}
    assert match_sentinel(spec, bytes([0xFF, 0xFF, 0xFF]), binary=True)
    assert not match_sentinel(spec, bytes([0xFF, 0xFF]), binary=True)
    assert not match_sentinel(spec, bytes([0x00, 0xFF, 0xFF, 0xFF]), binary=True)


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "sentinel/exact/v1", "values": []},
        {"id": "sentinel/exact/v1", "values": ["a", "a"]},
        {"id": "sentinel/exact/v1", "values": [str(i) for i in range(257)]},
        {"id": "sentinel/exact/v1", "values": ["x" * 1025]},
        {"id": "sentinel/exact/v1", "values": [3]},
    ],
)
def test_sentinel_config_limits(bad: dict) -> None:
    from decoy_engine.declared_casting import validate_sentinel_spec

    with pytest.raises(Exception) as exc:
        validate_sentinel_spec(bad, binary=False)
    assert getattr(exc.value, "code", None) == "declared_cast_sentinel_config_invalid"


def test_catalog_every_descriptor_declares_an_inverse_and_width() -> None:
    from decoy_engine.declared_casting import capability_manifest

    manifest = capability_manifest()
    assert manifest["catalog_version"] == 1
    ids = {f["id"] for f in manifest["formats"]}
    assert {
        "string/exact/v1", "integer/ascii/v1", "integer/zero-pad/v1", "decimal/plain/v1",
        "decimal/grouped/v1", "date/iso8601/v1", "date/strptime/v1", "date/strftime/v1",
        "currency/explicit/v1", "cobol/overpunch-ascii-trailing/v1",
        "cobol/zoned-ebcdic-trailing/v1", "cobol/comp-3/v1",
    } <= ids
    for f in manifest["formats"]:
        assert f["has_inverse"] is True
        assert f["physical_kind"] in {"text", "binary"}
        assert "width" in f and "logical" in f

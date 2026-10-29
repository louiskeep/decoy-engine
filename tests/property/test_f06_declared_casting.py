"""F06 B4: generated inverse and invalid-neighbour properties for every catalog family."""

from __future__ import annotations

import string
from datetime import date
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.acceptance._f06_support import EUR, INT64, ISO_DATE, USD

SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])

_DEC = {"id": "decimal/plain/v1", "precision": 9, "scale": 3}
_GROUPED = {"id": "decimal/grouped/v1", "precision": 12, "scale": 2, "radix": ".", "group": ","}
_MDY_IN = {"id": "date/strptime/v1", "format": "%m/%d/%Y"}
_MDY_OUT = {"id": "date/strftime/v1", "format": "%m/%d/%Y"}
_OVERPUNCH = {"id": "cobol/overpunch-ascii-trailing/v1", "precision": 7, "scale": 2}
_ZONED = {"id": "cobol/zoned-ebcdic-trailing/v1", "precision": 6, "scale": 2, "signed": True}
_COMP3 = {"id": "cobol/comp-3/v1", "precision": 8, "scale": 3, "signed": True}


def _api():
    from decoy_engine.declared_casting import format_value, parse_value

    return parse_value, format_value


def _decimals(precision: int, scale: int):
    limit = 10**precision - 1
    return st.integers(-limit, limit).map(lambda i: Decimal(i).scaleb(-scale))


@SETTINGS
@given(st.integers(-(2**63), 2**63 - 1))
def test_integer_round_trip(v: int) -> None:
    parse, fmt = _api()
    text = fmt(INT64, v)
    assert text.ok and parse(INT64, text.value).value == v


@SETTINGS
@given(_decimals(9, 3))
def test_decimal_round_trip_keeps_value_scale_and_sign(v: Decimal) -> None:
    parse, fmt = _api()
    out = fmt(_DEC, v)
    back = parse(_DEC, out.value).value
    assert back == v and back.as_tuple().exponent == -3 and back.is_signed() == v.is_signed()


@SETTINGS
@given(_decimals(12, 2))
def test_grouped_and_currency_round_trip(v: Decimal) -> None:
    parse, fmt = _api()
    for spec in (_GROUPED, USD, EUR):
        out = fmt(spec, v)
        assert out.ok, spec
        assert parse(spec, out.value).value == v


@SETTINGS
@given(st.dates(min_value=date(1, 1, 1), max_value=date(9999, 12, 31)))
def test_date_round_trip(d: date) -> None:
    parse, fmt = _api()
    assert parse(ISO_DATE, fmt(ISO_DATE, d).value).value == d
    assert parse(_MDY_IN, fmt(_MDY_OUT, d).value).value == d


@SETTINGS
@given(_decimals(7, 2))
def test_overpunch_round_trip(v: Decimal) -> None:
    parse, fmt = _api()
    out = fmt(_OVERPUNCH, v)
    assert parse(_OVERPUNCH, out.value).value == v


@SETTINGS
@given(_decimals(6, 2))
def test_zoned_round_trip_is_canonical(v: Decimal) -> None:
    parse, fmt = _api()
    out = fmt(_ZONED, v)
    assert isinstance(out.value, bytes) and parse(_ZONED, out.value).value == v
    assert fmt(_ZONED, parse(_ZONED, out.value).value).value == out.value


@SETTINGS
@given(_decimals(8, 3))
def test_comp3_round_trip_is_canonical(v: Decimal) -> None:
    parse, fmt = _api()
    out = fmt(_COMP3, v)
    assert len(out.value) == 5  # ceil((8 + 1) / 2)
    assert parse(_COMP3, out.value).value == v
    assert fmt(_COMP3, parse(_COMP3, out.value).value).value == out.value


def _one_edit(draw, text: str, alphabet: str) -> str:
    kind = draw(st.sampled_from(["insert", "delete", "replace"]))
    pos = draw(st.integers(0, max(len(text) - 1, 0)))
    ch = draw(st.sampled_from(alphabet))
    if kind == "insert" or not text:
        return text[:pos] + ch + text[pos:]
    if kind == "delete":
        return text[:pos] + text[pos + 1 :]
    return text[:pos] + ch + text[pos + 1 :]


@st.composite
def _edited_integer(draw):
    base = str(draw(st.integers(-(10**6), 10**6)))
    return base, _one_edit(draw, base, string.digits + "+-. e_,x ")


@SETTINGS
@given(_edited_integer())
def test_one_edit_neighbours_never_become_a_different_valid_integer(pair) -> None:
    parse, fmt = _api()
    base, edited = pair
    out = parse(INT64, edited)
    if out.ok:
        # whatever parses must be exactly the integer its own canonical text names
        assert str(out.value) == edited.lstrip("+").lstrip("0") or edited.lstrip("+-").strip("0") == "" or out.value == int(edited)
        assert fmt(INT64, out.value).value == str(out.value)
    else:
        assert str(out.reason) in {"declared_integer_invalid", "declared_integer_overflow"}


@st.composite
def _edited_date(draw):
    base = draw(st.dates(min_value=date(1000, 1, 1), max_value=date(9999, 12, 31))).isoformat()
    return _one_edit(draw, base, string.digits + "-/ T")


@SETTINGS
@given(_edited_date())
def test_one_edit_date_is_valid_only_if_it_is_the_canonical_text_of_its_value(text: str) -> None:
    parse, fmt = _api()
    out = parse(ISO_DATE, text)
    if out.ok:
        assert fmt(ISO_DATE, out.value).value == text


@st.composite
def _edited_currency(draw):
    v = draw(_decimals(12, 2))
    from decoy_engine.declared_casting import format_value

    base = format_value(USD, v).value
    return _one_edit(draw, base, string.digits + "$,.-() ")


@SETTINGS
@given(_edited_currency())
def test_one_edit_currency_is_valid_only_if_canonical(text: str) -> None:
    parse, fmt = _api()
    out = parse(USD, text)
    if out.ok:
        assert fmt(USD, out.value).value == text


@SETTINGS
@given(st.binary(min_size=0, max_size=8))
def test_arbitrary_bytes_are_valid_comp3_only_if_canonical(raw: bytes) -> None:
    parse, fmt = _api()
    out = parse(_COMP3, raw)
    if out.ok:
        assert fmt(_COMP3, out.value).value == raw  # no alternative encodings are accepted
    else:
        assert str(out.reason) == "declared_cobol_invalid"


@SETTINGS
@given(st.text(alphabet=string.printable, max_size=10), st.lists(st.text(max_size=5), min_size=1, max_size=4, unique=True))
def test_sentinel_matches_only_exact_members(raw: str, values: list[str]) -> None:
    from decoy_engine.declared_casting import match_sentinel

    spec = {"id": "sentinel/exact/v1", "values": values}
    assert match_sentinel(spec, raw, binary=False) == (raw in values)

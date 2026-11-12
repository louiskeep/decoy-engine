"""Vector batteries for the F06 mutation sentry.

`run_battery(source, kind)` executes a (possibly mutated) kernel module and returns True only if
every vector behaves exactly as specified. A mutant that removes a width/precision/range/shape
guard must make some vector fail (or crash), otherwise the guard is untested.
"""

from __future__ import annotations

import types
from datetime import date
from decimal import Decimal
from typing import Any

from decoy_engine.declared_casting import validate_format_spec


def _load(source: str, name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    exec(compile(source, name, "exec"), module.__dict__)  # noqa: S102 -- test-only mutant loader
    return module


def _spec(**fields: Any) -> Any:
    direction = fields.pop("direction", "parse")
    return validate_format_spec(fields, direction=direction)


def _code(outcome: Any) -> str | None:
    return None if outcome.ok else str(outcome.reason)


def _numeric(m: types.ModuleType) -> bool:
    i16 = _spec(id="integer/ascii/v1", signed=True, bit_width=16)
    u8 = _spec(id="integer/ascii/v1", signed=False, bit_width=8)
    zp = _spec(id="integer/zero-pad/v1", width=3, width_unit="code_points", direction="output")
    dec = _spec(id="decimal/plain/v1", precision=5, scale=2)
    checks = [
        m.parse_integer(i16, "32767").value == 32767,
        _code(m.parse_integer(i16, "32768")) == "declared_integer_overflow",
        m.parse_integer(i16, "-32768").value == -32768,
        _code(m.parse_integer(i16, "-32769")) == "declared_integer_overflow",
        _code(m.parse_integer(u8, "256")) == "declared_integer_overflow",
        _code(m.parse_integer(u8, "-1")) == "declared_integer_overflow",
        _code(m.format_integer(i16, 40000)) == "declared_integer_overflow",
        m.format_integer(zp, 42).value == "042",
        _code(m.format_integer(zp, 1000)) == "declared_output_width_overflow",
        _code(m.format_integer(zp, -420)) == "declared_output_width_overflow",
        m.parse_plain(dec, "999.99").value == Decimal("999.99"),
        _code(m.parse_plain(dec, "1000.00")) == "declared_decimal_overflow",
        _code(m.parse_plain(dec, "1.234")) == "declared_decimal_precision_loss",
        _code(m.format_plain(dec, Decimal("1.234"))) == "declared_decimal_precision_loss",
        _code(m.format_plain(dec, Decimal("1000.00"))) == "declared_decimal_overflow",
        m.format_plain(dec, Decimal("-0.50")).value == "-0.50",
    ]
    return all(checks)


def _cobol(m: types.ModuleType) -> bool:
    ovp = _spec(id="cobol/overpunch-ascii-trailing/v1", precision=5, scale=2)
    zon = _spec(id="cobol/zoned-ebcdic-trailing/v1", precision=3, scale=0, signed=True)
    zon_u = _spec(id="cobol/zoned-ebcdic-trailing/v1", precision=3, scale=0, signed=False)
    c3 = _spec(id="cobol/comp-3/v1", precision=4, scale=2, signed=True)
    c3_u = _spec(id="cobol/comp-3/v1", precision=3, scale=0, signed=False)
    bad = "declared_cobol_invalid"
    checks = [
        m.parse_overpunch(ovp, "0012L").value == Decimal("-1.23"),
        _code(m.parse_overpunch(ovp, "0012")) == bad,
        _code(m.parse_overpunch(ovp, "12ZZ{")) == bad,
        _code(m.parse_overpunch(ovp, "00123")) == bad,
        _code(m.parse_overpunch(ovp, "00012L")) == bad,
        m.parse_zoned(zon, bytes([0xF1, 0xF2, 0xD3])).value == -123,
        _code(m.parse_zoned(zon, bytes([0xF1, 0xF2]))) == bad,
        _code(m.parse_zoned(zon, bytes([0xF1, 0xF2, 0xF3, 0xC4]))) == bad,
        _code(m.parse_zoned(zon, bytes([0xF1, 0xFA, 0xC3]))) == bad,
        _code(m.parse_zoned(zon, bytes([0xF1, 0xF2, 0xF3]))) == bad,
        _code(m.parse_zoned(zon_u, bytes([0xF1, 0xF2, 0xC3]))) == bad,
        m.parse_comp3(c3, bytes([0x01, 0x23, 0x4C])).value == Decimal("12.34"),
        _code(m.parse_comp3(c3, bytes([0x01, 0x23]))) == bad,
        _code(m.parse_comp3(c3, bytes([0x00, 0x01, 0x23, 0x4C]))) == bad,
        _code(m.parse_comp3(c3, bytes([0x01, 0x2A, 0x4C]))) == bad,
        _code(m.parse_comp3(c3, bytes([0x11, 0x23, 0x4C]))) == bad,
        _code(m.parse_comp3(c3, bytes([0x01, 0x23, 0x4A]))) == bad,
        _code(m.parse_comp3(c3_u, bytes([0x12, 0x3C]))) == bad,
        m.parse_comp3(c3_u, bytes([0x12, 0x3F])).value == 123,
    ]
    return all(checks)


def _date(m: types.ModuleType) -> bool:
    iso = _spec(id="date/iso8601/v1")
    checks = [
        m.parse_date(iso, "2026-02-01").value == date(2026, 2, 1),
        _code(m.parse_date(iso, "2026-2-1")) == "declared_date_invalid",
        _code(m.parse_date(iso, "2026-02-01x")) == "declared_date_invalid",
        _code(m.parse_date(iso, "2026-02-30")) == "declared_date_invalid",
    ]
    return all(checks)


_BATTERIES = {"numeric": _numeric, "cobol": _cobol, "date": _date}


def run_battery(source: str, kind: str) -> bool:
    try:
        return bool(_BATTERIES[kind](_load(source, f"_f06_mutant_{kind}")))
    except Exception:
        return False

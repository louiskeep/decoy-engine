"""COBOL field codecs: ASCII trailing overpunch, EBCDIC zoned decimal, COMP-3 packed decimal.

Table-driven from the IBM conventions (overpunch characters, zoned zone nibbles, packed digit and
sign nibbles); Python ships no such codec. The implied decimal point is applied as
``Decimal(i).scaleb(-s)`` in an engine-pinned context (``_numeric.scaled``), never an ambient one.
Strict V1: every digit nibble must be 0-9, only the documented sign nibbles are admitted, a byte
count that is not exactly the declared width is rejected, and output is canonical (C/D/F).
"""

from __future__ import annotations

from typing import Any

from decoy_engine import reasons
from decoy_engine.declared_casting._numeric import scaled, unscale
from decoy_engine.declared_casting._types import CastOutcome, fail, succeed

_R = reasons.runtime
POSITIVE = "{ABCDEFGHI"
NEGATIVE = "}JKLMNOPQR"
_OVERPUNCH = {
    **{c: (d, False) for d, c in enumerate(POSITIVE)},
    **{c: (d, True) for d, c in enumerate(NEGATIVE)},
}
_ZONED_POSITIVE = frozenset({0xA, 0xC, 0xE})
_ZONED_NEGATIVE = frozenset({0xB, 0xD})
_BAD = _R.DECLARED_COBOL_INVALID


def comp3_length(precision: int) -> int:
    return (precision + 2) // 2  # ceil((precision + 1) / 2)


def _digits_value(digits: str, spec: Any, negative: bool) -> CastOutcome:
    return succeed(scaled(int(digits), spec.scale, negative=negative))


def parse_overpunch(spec: Any, raw: Any) -> CastOutcome:
    if not isinstance(raw, str):
        return fail(_BAD)
    if len(raw) != spec.precision:  # guard:cobol_length
        return fail(_BAD)
    body, last = raw[:-1], raw[-1]
    mapped = _OVERPUNCH.get(last)
    body_ok = body.isascii() and (body == "" or body.isdigit())
    if mapped is None or not body_ok:  # guard:cobol_digit
        return fail(_BAD)
    digit, negative = mapped
    return _digits_value(body + str(digit), spec, negative)


def format_overpunch(spec: Any, value: Any) -> CastOutcome:
    got = unscale(value, spec.scale, spec.precision)
    if isinstance(got, CastOutcome):
        return got
    unscaled, negative = got
    digits = str(unscaled).rjust(spec.precision, "0")
    table = NEGATIVE if negative else POSITIVE
    return succeed(digits[:-1] + table[int(digits[-1])])


def parse_zoned(spec: Any, raw: Any) -> CastOutcome:
    if not isinstance(raw, (bytes, bytearray)):
        return fail(_BAD)
    if len(raw) != spec.precision:  # guard:cobol_length
        return fail(_BAD)
    digits = []
    for i, byte in enumerate(raw):
        zone, digit = byte >> 4, byte & 0xF
        if digit > 9:  # guard:cobol_digit
            return fail(_BAD)
        if i < len(raw) - 1 and zone != 0xF:
            return fail(_BAD)
        digits.append(str(digit))
    zone = raw[-1] >> 4
    if spec.signed:
        if zone not in _ZONED_POSITIVE and zone not in _ZONED_NEGATIVE:  # guard:cobol_sign
            return fail(_BAD)
        negative = zone in _ZONED_NEGATIVE
    else:
        if zone != 0xF:  # guard:cobol_sign
            return fail(_BAD)
        negative = False
    return _digits_value("".join(digits), spec, negative)


def format_zoned(spec: Any, value: Any) -> CastOutcome:
    got = unscale(value, spec.scale, spec.precision)
    if isinstance(got, CastOutcome):
        return got
    unscaled, negative = got
    if negative and not spec.signed:
        return fail(_BAD)
    digits = str(unscaled).rjust(spec.precision, "0")
    out = bytearray(0xF0 | int(c) for c in digits)
    out[-1] = (((0xD if negative else 0xC) if spec.signed else 0xF) << 4) | int(digits[-1])
    return succeed(bytes(out))


def parse_comp3(spec: Any, raw: Any) -> CastOutcome:
    if not isinstance(raw, (bytes, bytearray)):
        return fail(_BAD)
    if len(raw) != comp3_length(spec.precision):  # guard:cobol_length
        return fail(_BAD)
    nibbles = [n for byte in raw for n in (byte >> 4, byte & 0xF)]
    *digit_nibbles, sign = nibbles
    if any(n > 9 for n in digit_nibbles):  # guard:cobol_digit
        return fail(_BAD)
    if len(digit_nibbles) > spec.precision and digit_nibbles[0] != 0:  # guard:comp3_pad_nibble
        return fail(_BAD)
    if spec.signed:
        if sign not in (0xC, 0xD):  # guard:cobol_sign
            return fail(_BAD)
    elif sign != 0xF:  # guard:cobol_sign
        return fail(_BAD)
    return _digits_value("".join(map(str, digit_nibbles)), spec, sign == 0xD)


def format_comp3(spec: Any, value: Any) -> CastOutcome:
    got = unscale(value, spec.scale, spec.precision)
    if isinstance(got, CastOutcome):
        return got
    unscaled, negative = got
    if negative and not spec.signed:
        return fail(_BAD)
    length = comp3_length(spec.precision)
    sign = (0xD if negative else 0xC) if spec.signed else 0xF
    digits = str(unscaled).rjust(2 * length - 1, "0")
    packed = int(digits, 16) * 16 + sign  # each decimal digit is one hex nibble
    return succeed(packed.to_bytes(length, "big"))

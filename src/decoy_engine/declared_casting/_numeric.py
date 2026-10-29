"""Strict integer, decimal, grouped-decimal and currency kernels.

Exactness comes from integer arithmetic on validated ASCII digits: a decimal is its unscaled
integer ``i`` and declared scale ``s``, built as ``Decimal(i).scaleb(-s)`` inside an engine-pinned
``decimal`` context so the caller's or thread's ambient precision, rounding and traps cannot
change a result (``scaleb`` rounds to the *ambient* precision otherwise). Nothing here uses
binary floating point, rounding, ``quantize`` or locale data. Pattern sources: Python ``decimal``
(exact base-10), Arrow ``decimal128`` bounds (precision <= 38), Unicode TR35 (every symbol is
declared, none is inferred).

Lines tagged ``# guard:<name>`` are the load-bearing comparisons; the mutation sentry removes each
one in turn and the vector battery must notice.
"""

from __future__ import annotations

import decimal
import re
from decimal import Decimal
from typing import Any

from decoy_engine import reasons
from decoy_engine.declared_casting._types import CastOutcome, fail, succeed

_R = reasons.runtime
_INT = re.compile(r"[+-]?[0-9]+", re.ASCII)
_PLAIN = re.compile(r"([+-]?)([0-9]+)(?:\.([0-9]+))?", re.ASCII)
MAX_TEXT = 128  # a longer lexeme cannot fit a 38-digit field; bounding it bounds the work

_PINNED = decimal.Context(
    prec=80,
    rounding=decimal.ROUND_HALF_EVEN,
    Emin=-200,
    Emax=200,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[
        decimal.InvalidOperation,
        decimal.Inexact,
        decimal.Rounded,
        decimal.Overflow,
        decimal.Underflow,
        decimal.DivisionByZero,
    ],
)


def scaled(unscaled: int, scale: int, *, negative: bool) -> Decimal:
    """``unscaled`` x 10**-scale exactly, keeping a negative zero."""
    with decimal.localcontext(_PINNED):
        value = Decimal(unscaled).scaleb(-scale)
        return value.copy_negate() if negative else value


def unscale(value: Any, scale: int, precision: int) -> tuple[int, bool] | CastOutcome:
    """Exact ``(unscaled, negative)`` of a Decimal/int at ``scale``, or the failing outcome."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        return fail(_R.DECLARED_DECIMAL_INVALID)
    d = value if isinstance(value, Decimal) else Decimal(value)
    sign, digits, exponent = d.as_tuple()
    if not isinstance(exponent, int) or len(digits) > 2 * MAX_TEXT or abs(exponent) > 2 * MAX_TEXT:
        return fail(_R.DECLARED_DECIMAL_INVALID)
    coefficient = int("".join(map(str, digits)) or "0")
    shift = exponent + scale
    if shift >= 0:
        unscaled = coefficient * 10**shift
    else:
        unscaled, remainder = divmod(coefficient, 10**-shift)
        if remainder:  # guard:decimal_scale_loss_format
            return fail(_R.DECLARED_DECIMAL_PRECISION_LOSS)
    if unscaled >= 10**precision:  # guard:decimal_precision_format
        return fail(_R.DECLARED_DECIMAL_OVERFLOW)
    return unscaled, bool(sign)


# -- integer -----------------------------------------------------------------------------------


def int_bounds(signed: bool, bit_width: int) -> tuple[int, int]:
    return (
        (-(1 << (bit_width - 1)), (1 << (bit_width - 1)) - 1)
        if signed
        else (0, (1 << bit_width) - 1)
    )


def parse_integer(spec: Any, raw: Any) -> CastOutcome:
    if not isinstance(raw, str) or len(raw) > MAX_TEXT or not _INT.fullmatch(raw):
        return fail(_R.DECLARED_INTEGER_INVALID)
    significant = raw.lstrip("+-").lstrip("0")
    lo, hi = int_bounds(spec.signed, spec.bit_width)
    if len(significant) > len(str(hi)) + 1:  # guard:integer_digits
        return fail(_R.DECLARED_INTEGER_OVERFLOW)
    value = int(raw)
    if not lo <= value <= hi:  # guard:integer_range
        return fail(_R.DECLARED_INTEGER_OVERFLOW)
    return succeed(value)


def format_integer(spec: Any, value: Any) -> CastOutcome:
    if isinstance(value, bool) or not isinstance(value, int):
        return fail(_R.DECLARED_INTEGER_INVALID)
    lo, hi = int_bounds(spec.signed, spec.bit_width)
    if not lo <= value <= hi:  # guard:integer_range_format
        return fail(_R.DECLARED_INTEGER_OVERFLOW)
    text = str(value)
    if spec.id == "integer/zero-pad/v1":
        sign, digits = ("-", text[1:]) if value < 0 else ("", text)
        room = spec.width - len(sign)  # the sign consumes width; unit is irrelevant for ASCII
        if len(digits) > room:  # guard:width_overflow_zero_pad
            return fail(_R.DECLARED_OUTPUT_WIDTH_OVERFLOW)
        return succeed(sign + digits.rjust(room, "0"))
    return succeed(text)


# -- decimal -----------------------------------------------------------------------------------


def parse_plain(spec: Any, raw: Any) -> CastOutcome:
    if not isinstance(raw, str) or len(raw) > MAX_TEXT:
        return fail(_R.DECLARED_DECIMAL_INVALID)
    m = _PLAIN.fullmatch(raw)
    if m is None:
        return fail(_R.DECLARED_DECIMAL_INVALID)
    sign, whole, frac = m.group(1), m.group(2), m.group(3) or ""
    return _from_parts(spec, sign == "-", whole, frac, _DECIMAL_LOSS, _DECIMAL_OVERFLOW)


_DECIMAL_LOSS = _R.DECLARED_DECIMAL_PRECISION_LOSS
_DECIMAL_OVERFLOW = _R.DECLARED_DECIMAL_OVERFLOW


def _from_parts(
    spec: Any, negative: bool, whole: str, frac: str, loss: Any, overflow: Any
) -> CastOutcome:
    if len(frac) > spec.scale:  # guard:decimal_scale_loss
        return fail(loss)
    unscaled = int(whole + frac.ljust(spec.scale, "0"))
    if unscaled >= 10**spec.precision:  # guard:decimal_precision
        return fail(overflow)
    return succeed(scaled(unscaled, spec.scale, negative=negative))


def _group(whole: str, sep: str | None) -> str:
    if sep is None or len(whole) <= 3:
        return whole
    head = len(whole) % 3 or 3
    return sep.join([whole[:head], *(whole[i : i + 3] for i in range(head, len(whole), 3))])


def render_number(unscaled: int, scale: int, radix: str, group: str | None) -> str:
    digits = str(unscaled).rjust(scale + 1, "0")
    whole, frac = digits[: len(digits) - scale], digits[len(digits) - scale :]
    return _group(whole, group) + (radix + frac if scale else "")


def format_plain(spec: Any, value: Any) -> CastOutcome:
    got = unscale(value, spec.scale, spec.precision)
    if isinstance(got, CastOutcome):
        return got
    unscaled, negative = got
    group = getattr(spec, "group", None)
    text = render_number(unscaled, spec.scale, getattr(spec, "radix", "."), group)
    return succeed(("-" if negative else "") + text)


def _number_pattern(spec: Any) -> re.Pattern[str]:
    radix = re.escape(spec.radix)
    if spec.group is None:
        whole = r"[0-9]+"
    else:
        whole = rf"(?:0|[1-9][0-9]{{0,2}}(?:{re.escape(spec.group)}[0-9]{{3}})*|[1-9][0-9]*)"
    frac = rf"{radix}[0-9]{{{spec.scale}}}" if spec.scale else ""
    return re.compile(rf"(?P<whole>{whole})(?P<frac>{frac})", re.ASCII)


def _grouped_digits(spec: Any, number: str) -> tuple[str, str] | None:
    m = _number_pattern(spec).fullmatch(number)
    if m is None:
        return None
    whole = m.group("whole")
    if spec.group is not None:
        if re.fullmatch(r"[1-9][0-9]{3,}", whole):  # ungrouped but long: not canonical
            return None
        whole = whole.replace(spec.group, "")
    return whole, m.group("frac")[1:]


def parse_grouped(spec: Any, raw: Any) -> CastOutcome:
    if not isinstance(raw, str) or len(raw) > MAX_TEXT:
        return fail(_R.DECLARED_DECIMAL_INVALID)
    negative = raw.startswith("-")
    parts = _grouped_digits(spec, raw[1:] if negative or raw.startswith("+") else raw)
    if parts is None:
        return fail(_R.DECLARED_DECIMAL_INVALID)
    return _from_parts(spec, negative, parts[0], parts[1], _DECIMAL_LOSS, _DECIMAL_OVERFLOW)


# -- currency ----------------------------------------------------------------------------------


def parse_currency(spec: Any, raw: Any) -> CastOutcome:
    bad = _R.DECLARED_CURRENCY_INVALID
    if not isinstance(raw, str) or len(raw) > MAX_TEXT:
        return fail(bad)
    body, negative = raw, False
    if spec.sign_style == "leading_minus":
        if body.startswith("-"):
            body, negative = body[1:], True
    elif body.startswith("(") and body.endswith(")"):
        body, negative = body[1:-1], True
    if spec.token_position == "prefix":  # noqa: S105 -- a currency-symbol position, not a credential
        if not body.startswith(spec.token):
            return fail(bad)
        number = body[len(spec.token) :]
    else:
        if not body.endswith(spec.token):
            return fail(bad)
        number = body[: len(body) - len(spec.token)]
    parts = _grouped_digits(spec, number)
    if parts is None:
        return fail(bad)
    return _from_parts(spec, negative, parts[0], parts[1], bad, bad)


def format_currency(spec: Any, value: Any) -> CastOutcome:
    got = unscale(value, spec.scale, spec.precision)
    if isinstance(got, CastOutcome):
        return got
    unscaled, negative = got
    number = render_number(unscaled, spec.scale, spec.radix, spec.group)
    body = spec.token + number if spec.token_position == "prefix" else number + spec.token  # noqa: S105
    if not negative:
        return succeed(body)
    return succeed("-" + body if spec.sign_style == "leading_minus" else "(" + body + ")")

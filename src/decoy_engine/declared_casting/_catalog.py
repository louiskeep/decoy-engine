"""The only format descriptor registry (``FormatDescriptor``, keyed by ``(format_id, direction)``).

A format becomes usable only by being registered here: unknown ids never fall through to a
default. Each descriptor owns its pydantic shape (``decoy_engine.contracts.declared_casting``), its
parse/format kernels, the id of its inverse, its width rule, its physical kind and registered
vectors. The formatter always runs the parser as an exact-inverse oracle on what it emitted, so a
representation that does not read back to the same logical value is never produced.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import Any

from decoy_engine import reasons
from decoy_engine.contracts import declared_casting as C  # noqa: N812 -- short alias for the contract namespace
from decoy_engine.declared_casting import _cobol, _date, _numeric
from decoy_engine.declared_casting._types import CastOutcome, fail, succeed

_R = reasons.runtime
Kernel = Callable[[Any, Any], CastOutcome]


@dataclass(frozen=True)
class FormatDescriptor:
    format_id: str
    direction: str
    model: type
    physical_kind: str
    logical: str
    kernel: Kernel
    inverse: str
    width_rule: str  # exact | bounded | unknown
    vectors: tuple[tuple[dict[str, Any], Any, Any], ...]


def _string_exact(spec: Any, value: Any) -> CastOutcome:
    # Strings cannot fail by value; a non-text carrier is a route-level type mismatch.
    return (
        succeed(value)
        if isinstance(value, str)
        else fail(reasons.route.DECLARED_CAST_INPUT_TYPE_INCOMPATIBLE)
    )


_INT = {"id": "integer/ascii/v1", "signed": True, "bit_width": 16}
_DEC = {"id": "decimal/plain/v1", "precision": 5, "scale": 2}
_GRP = {"id": "decimal/grouped/v1", "precision": 8, "scale": 2, "radix": ".", "group": ","}
_CUR = {
    "id": "currency/explicit/v1",
    "currency_code": "USD",
    "token": "$",
    "token_position": "prefix",
    "sign_style": "leading_minus",
    "precision": 8,
    "scale": 2,
    "radix": ".",
    "group": ",",
}
_OVP = {"id": "cobol/overpunch-ascii-trailing/v1", "precision": 5, "scale": 2}
_ZON = {"id": "cobol/zoned-ebcdic-trailing/v1", "precision": 3, "scale": 0, "signed": True}
_C3 = {"id": "cobol/comp-3/v1", "precision": 5, "scale": 0, "signed": True}
_STRP = {"id": "date/strptime/v1", "format": "%m/%d/%Y"}
_STRF = {"id": "date/strftime/v1", "format": "%m/%d/%Y"}
_ZP = {"id": "integer/zero-pad/v1", "width": 4, "width_unit": "code_points"}

_PARSE: dict[str, tuple[Kernel, str, str, str, tuple[tuple[dict[str, Any], Any, Any], ...]]] = {
    "string/exact/v1": (
        _string_exact,
        "text",
        "bounded_by_source",
        "unknown",
        (({"id": "string/exact/v1"}, "ab c", "ab c"),),
    ),
    "integer/ascii/v1": (
        _numeric.parse_integer,
        "text",
        "integer/ascii/v1",
        "bounded",
        ((_INT, "042", 42),),
    ),
    "decimal/plain/v1": (
        _numeric.parse_plain,
        "text",
        "decimal/plain/v1",
        "bounded",
        ((_DEC, "1.5", Decimal("1.50")),),
    ),
    "decimal/grouped/v1": (
        _numeric.parse_grouped,
        "text",
        "decimal/grouped/v1",
        "bounded",
        ((_GRP, "1,234.50", Decimal("1234.50")),),
    ),
    "date/iso8601/v1": (
        _date.parse_date,
        "text",
        "date/iso8601/v1",
        "exact",
        (({"id": "date/iso8601/v1"}, "2026-02-01", date(2026, 2, 1)),),
    ),
    "date/strptime/v1": (
        _date.parse_date,
        "text",
        "date/strftime/v1",
        "exact",
        ((_STRP, "01/02/2026", date(2026, 1, 2)),),
    ),
    "currency/explicit/v1": (
        _numeric.parse_currency,
        "text",
        "currency/explicit/v1",
        "bounded",
        ((_CUR, "$1,234.50", Decimal("1234.50")),),
    ),
    "cobol/overpunch-ascii-trailing/v1": (
        _cobol.parse_overpunch,
        "text",
        "cobol/overpunch-ascii-trailing/v1",
        "exact",
        ((_OVP, "0012L", Decimal("-1.23")),),
    ),
    "cobol/zoned-ebcdic-trailing/v1": (
        _cobol.parse_zoned,
        "binary",
        "cobol/zoned-ebcdic-trailing/v1",
        "exact",
        ((_ZON, bytes([0xF1, 0xF2, 0xD3]), Decimal(-123)),),
    ),
    "cobol/comp-3/v1": (
        _cobol.parse_comp3,
        "binary",
        "cobol/comp-3/v1",
        "exact",
        ((_C3, bytes([0x12, 0x34, 0x5C]), Decimal(12345)),),
    ),
}
_FORMAT: dict[str, tuple[Kernel, str, str, tuple[tuple[dict[str, Any], Any, Any], ...]]] = {
    "string/exact/v1": (
        _string_exact,
        "string/exact/v1",
        "unknown",
        (({"id": "string/exact/v1"}, "ab c", "ab c"),),
    ),
    "integer/ascii/v1": (
        _numeric.format_integer,
        "integer/ascii/v1",
        "bounded",
        ((_INT, 42, "42"),),
    ),
    "integer/zero-pad/v1": (
        _numeric.format_integer,
        "integer/ascii/v1",
        "bounded",
        ((_ZP, 42, "0042"),),
    ),
    "decimal/plain/v1": (
        _numeric.format_plain,
        "decimal/plain/v1",
        "bounded",
        ((_DEC, Decimal("1.5"), "1.50"),),
    ),
    "decimal/grouped/v1": (
        _numeric.format_plain,
        "decimal/grouped/v1",
        "bounded",
        ((_GRP, Decimal("1234.5"), "1,234.50"),),
    ),
    "date/iso8601/v1": (
        _date.format_date,
        "date/iso8601/v1",
        "exact",
        (({"id": "date/iso8601/v1"}, date(2026, 2, 1), "2026-02-01"),),
    ),
    "date/strftime/v1": (
        _date.format_date,
        "date/strptime/v1",
        "exact",
        ((_STRF, date(2026, 1, 2), "01/02/2026"),),
    ),
    "currency/explicit/v1": (
        _numeric.format_currency,
        "currency/explicit/v1",
        "bounded",
        ((_CUR, Decimal("1234.5"), "$1,234.50"),),
    ),
    "cobol/overpunch-ascii-trailing/v1": (
        _cobol.format_overpunch,
        "cobol/overpunch-ascii-trailing/v1",
        "exact",
        ((_OVP, Decimal("-1.23"), "0012L"),),
    ),
    "cobol/zoned-ebcdic-trailing/v1": (
        _cobol.format_zoned,
        "cobol/zoned-ebcdic-trailing/v1",
        "exact",
        ((_ZON, Decimal(-123), bytes([0xF1, 0xF2, 0xD3])),),
    ),
    "cobol/comp-3/v1": (
        _cobol.format_comp3,
        "cobol/comp-3/v1",
        "exact",
        ((_C3, Decimal(12345), bytes([0x12, 0x34, 0x5C])),),
    ),
}


def _build() -> tuple[FormatDescriptor, ...]:
    out: list[FormatDescriptor] = []
    for fid, (kernel, kind, inverse, rule, vectors) in _PARSE.items():
        model = C.PARSE_MODELS[fid]
        out.append(
            FormatDescriptor(
                fid, "parse", model, kind, model.logical, kernel, inverse, rule, vectors
            )
        )
    for fid, (kernel, inverse, rule, vectors) in _FORMAT.items():
        model = C.OUTPUT_MODELS[fid]
        kind = "binary" if model.binary else "text"
        out.append(
            FormatDescriptor(
                fid, "output", model, kind, model.logical, kernel, inverse, rule, vectors
            )
        )
    return tuple(sorted(out, key=lambda d: (d.format_id, d.direction)))


_DESCRIPTORS = _build()
_BY_KEY = {(d.format_id, d.direction): d for d in _DESCRIPTORS}


def descriptors() -> tuple[FormatDescriptor, ...]:
    return _DESCRIPTORS


def descriptor_for(format_id: str, direction: str) -> FormatDescriptor:
    try:
        return _BY_KEY[(format_id, direction)]
    except KeyError:
        raise C.DeclaredCastContractError(reasons.plan.DECLARED_CAST_FORMAT_UNKNOWN) from None


# -- resolved specs ----------------------------------------------------------------------------


def _resolve(spec: Any, direction: str) -> Any:
    if isinstance(spec, C.ContractModel):
        return spec
    return _resolved_json(json.dumps(spec, sort_keys=True, default=str), direction)


@lru_cache(maxsize=512)
def _resolved_json(blob: str, direction: str) -> Any:
    return C.validate_format_spec(json.loads(blob), direction=direction)  # type: ignore[arg-type]


def validate_format_spec(spec: Any, *, direction: str = "parse") -> Any:
    return C.validate_format_spec(spec, direction=direction)  # type: ignore[arg-type]


def validate_sentinel_spec(spec: Any, *, binary: bool) -> Any:
    return C.validate_sentinel_spec(spec, binary=binary)


# -- widths ------------------------------------------------------------------------------------


def _measure(text: str, unit: str | None) -> int:
    return len(text.encode("utf-8")) if unit == "bytes" else len(text)


def output_width(spec: Any) -> int | None:
    """The width a fixed-width target can rely on: declared, else statically exact, else unknown."""
    model = _resolve(spec, "output")
    if getattr(model, "width", None) is not None:
        return model.width  # type: ignore[no-any-return]
    return _static_width(model)


def _static_width(model: Any) -> int | None:
    fid = model.id
    if fid == "date/iso8601/v1":
        return 10
    if fid == "date/strftime/v1":
        return C.date_width(model.format)
    if fid == "cobol/overpunch-ascii-trailing/v1" or fid == "cobol/zoned-ebcdic-trailing/v1":
        return model.precision  # type: ignore[no-any-return]
    if fid == "cobol/comp-3/v1":
        return _cobol.comp3_length(model.precision)
    return None


def max_rendered_width(spec: Any) -> int | None:
    """The widest unpadded rendering in the spec's width unit, or ``None`` when unbounded."""
    model = _resolve(spec, "output")
    fixed = _static_width(model)
    if fixed is not None:
        return fixed
    fid, unit = model.id, getattr(model, "width_unit", None) or "code_points"
    if fid in ("integer/ascii/v1", "integer/zero-pad/v1"):
        lo, hi = _numeric.int_bounds(model.signed, model.bit_width)
        return max(len(str(lo)), len(str(hi)))
    if fid in ("decimal/plain/v1", "decimal/grouped/v1", "currency/explicit/v1"):
        whole = max(model.precision - model.scale, 1)
        number = whole + (model.scale + 1 if model.scale else 0)
        if getattr(model, "group", None):
            number += (whole - 1) // 3
        if fid != "currency/explicit/v1":
            return number + 1  # sign
        return number + 2 + _measure(model.token, unit)  # parentheses are the wider sign style
    return None


# -- compiled kernels --------------------------------------------------------------------------


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        return a == b and a.is_signed() == b.is_signed()
    return bool(a == b) and type(a) is type(b)


def _normalized(value: Any, twin_spec: Any) -> Any:
    """The logical value as the parser would return it (int -> Decimal at the declared scale)."""
    if isinstance(
        twin_spec, (C.DecimalPlain, C.DecimalGrouped, C.Currency, C.Overpunch, C.Zoned, C.Comp3)
    ):
        got = _numeric.unscale(value, twin_spec.scale, twin_spec.precision)
        if isinstance(got, CastOutcome):
            return None
        return _numeric.scaled(got[0], twin_spec.scale, negative=got[1])
    return value


def compile_parser(spec: Any) -> Callable[[Any], CastOutcome]:
    model = _resolve(spec, "parse")
    kernel = descriptor_for(model.id, "parse").kernel
    return lambda raw: kernel(model, raw)


def compile_formatter(spec: Any) -> Callable[[Any], CastOutcome]:
    model = _resolve(spec, "output")
    desc = descriptor_for(model.id, "output")
    twin_id = desc.inverse
    twin_fields = {
        k: getattr(model, k) for k in C.PARSE_MODELS[twin_id].model_fields if hasattr(model, k)
    }
    twin = C.PARSE_MODELS[twin_id].model_validate({**twin_fields, "id": twin_id})
    parse = descriptor_for(twin_id, "parse").kernel
    width = getattr(model, "width", None)
    unit, pad, align = (
        getattr(model, "width_unit", None),
        getattr(model, "pad", " "),
        getattr(model, "align", "right"),
    )
    is_zero_pad = model.id == "integer/zero-pad/v1"
    kernel = desc.kernel

    def run(value: Any) -> CastOutcome:
        out = kernel(model, value)
        if not out.ok or isinstance(out.value, bytes):
            return _inverse_checked(out, parse, twin, value, None, None, pad)
        text = out.value
        if width is not None and not is_zero_pad:
            room = width - _measure(text, unit)
            if room < 0:  # guard:width_overflow
                return fail(_R.DECLARED_OUTPUT_WIDTH_OVERFLOW)
            text = text + pad * room if align == "left" else pad * room + text
        padded = width is not None and not is_zero_pad
        return _inverse_checked(
            succeed(text), parse, twin, value, text, align if padded else None, pad
        )

    return run


def _inverse_checked(
    out: CastOutcome,
    parse: Kernel,
    twin: Any,
    value: Any,
    text: str | None,
    strip: str | None,
    pad: str,
) -> CastOutcome:
    if not out.ok:
        return out
    emitted = out.value
    probe = emitted
    if strip is not None:
        probe = emitted.lstrip(pad) if strip == "right" else emitted.rstrip(pad)
    back = parse(twin, probe)
    expected = value if isinstance(twin, C.StringExact) else _normalized(value, twin)
    if not back.ok or not _same(back.value, expected):
        # A string whose edge equals the pad cannot be told from padding; nothing else may land here.
        if isinstance(twin, C.StringExact):
            return fail(_R.DECLARED_OUTPUT_WIDTH_OVERFLOW)
        raise C.DeclaredCastContractError(
            reasons.plan.DECLARED_CAST_CONTRACT_INVALID,
            "formatter is not the exact inverse of its parser.",
        )
    return out


@lru_cache(maxsize=512)
def _cached_parser(blob: str) -> Callable[[Any], CastOutcome]:
    return compile_parser(json.loads(blob))


@lru_cache(maxsize=512)
def _cached_formatter(blob: str) -> Callable[[Any], CastOutcome]:
    return compile_formatter(json.loads(blob))


def _key(spec: Any) -> str:
    return json.dumps(
        spec if not isinstance(spec, C.ContractModel) else spec.model_dump(), sort_keys=True, default=str
    )


def parse_value(spec: Any, raw: Any) -> CastOutcome:
    return _cached_parser(_key(spec))(raw)


def format_value(spec: Any, value: Any) -> CastOutcome:
    return _cached_formatter(_key(spec))(value)


# -- sentinel ----------------------------------------------------------------------------------


def match_sentinel(spec: Any, raw: Any, *, binary: bool) -> bool:
    """Exact match on the framed token; no trim, fold, normalization or pad removal."""
    if raw is None:
        return False
    model = (
        spec
        if isinstance(spec, C.ExactSentinelSetV1)
        else C.validate_sentinel_spec(spec, binary=binary)
    )
    tokens = model.decoded(binary=binary)
    if binary:
        return isinstance(raw, (bytes, bytearray)) and bytes(raw) in tokens
    return isinstance(raw, str) and raw in tokens


# -- exported capability manifest --------------------------------------------------------------


def capability_manifest() -> dict[str, Any]:
    formats: dict[str, dict[str, Any]] = {}
    for d in _DESCRIPTORS:
        entry = formats.setdefault(
            d.format_id,
            {
                "id": d.format_id,
                "directions": [],
                "physical_kind": d.physical_kind,
                "logical": d.logical,
                "has_inverse": True,
                "width": {},
                "schemas": {},
            },
        )
        entry["directions"].append(d.direction)
        entry["width"][d.direction] = d.width_rule
        entry["schemas"][d.direction] = d.model.model_json_schema()
    return {
        "catalog_version": C.CATALOG_VERSION,
        "formats": [formats[k] for k in sorted(formats)],
        "sentinel": {
            "id": "sentinel/exact/v1",
            "max_values": C.MAX_SENTINELS,
            "max_bytes": C.MAX_SENTINEL_BYTES,
        },
        "limits": {"max_precision": C.MAX_PRECISION, "max_currency_token_bytes": C.MAX_TOKEN_BYTES},
    }

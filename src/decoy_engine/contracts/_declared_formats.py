"""The closed catalog of F06 format shapes (parse side and output side) and their registries."""

from __future__ import annotations

import itertools
import re
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import (
    Discriminator,
    Tag,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from decoy_engine import reasons
from decoy_engine.contracts._declared_base import (
    _P,
    INTEGER_WIDTHS,
    MAX_TOKEN_BYTES,
    Align,
    ContractModel,
    DeclaredCastContractError,
    Direction,
    WidthUnit,
    _Precision,
    _Separators,
    violation,
)

# -- parse-side formats ------------------------------------------------------------------------


class StringExact(ContractModel):
    id: Literal["string/exact/v1"]
    logical = "string"


class IntegerAscii(ContractModel):
    id: Literal["integer/ascii/v1"]
    signed: bool = True
    bit_width: int = 64
    logical = "integer"

    @field_validator("bit_width", mode="before")
    @classmethod
    def _bits(cls, v: Any) -> int:
        if isinstance(v, bool) or v not in INTEGER_WIDTHS:
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "bit_width must be one of 8, 16, 32, 64."
            )
        return v


class DecimalPlain(_Precision):
    id: Literal["decimal/plain/v1"]
    logical = "decimal"


class DecimalGrouped(_Precision, _Separators):
    id: Literal["decimal/grouped/v1"]
    group: str | None = ","
    logical = "decimal"

    @model_validator(mode="after")
    def _needs_group(self) -> DecimalGrouped:
        if self.group is None:
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED,
                "decimal/grouped/v1 requires a group separator.",
            )
        return self


# Date directives: exactly one each of year, month, day, a literal between any two directives,
# and only separators that cannot be mistaken for a field. No locale name or platform extension.
_DATE_LITERALS = "-/. :_,"
_DIRECTIVE = re.compile(r"%(.)")


def date_pattern(fmt: str) -> list[str]:
    """Split a restricted date format into ``%Y|%m|%d`` tokens and literal characters."""
    parts: list[str] = []
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch == "%":
            token = fmt[i : i + 2]
            if token not in ("%Y", "%m", "%d"):
                raise violation(
                    _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "date directive is outside %Y %m %d."
                )
            parts.append(token)
            i += 2
        elif ch in _DATE_LITERALS:
            parts.append(ch)
            i += 1
        else:
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "date format literal is not a separator."
            )
    tokens = [p for p in parts if p.startswith("%")]
    if sorted(tokens) != ["%Y", "%d", "%m"]:
        raise violation(
            _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "date format needs each of %Y, %m and %d once."
        )
    for a, b in itertools.pairwise(parts):
        if a.startswith("%") and b.startswith("%"):
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "date fields need a separator between them."
            )
    return parts


def date_width(fmt: str) -> int:
    return sum(4 if p == "%Y" else 2 if p in ("%m", "%d") else 1 for p in date_pattern(fmt))


class DateIso(ContractModel):
    id: Literal["date/iso8601/v1"]
    logical = "date"


class DateStrptime(ContractModel):
    id: Literal["date/strptime/v1"]
    format: str
    logical = "date"

    @field_validator("format", mode="before")
    @classmethod
    def _fmt(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise violation(_P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "date format must be text.")
        date_pattern(v)
        return v


class Currency(_Precision, _Separators):
    id: Literal["currency/explicit/v1"]
    currency_code: str
    token: str
    token_position: Literal["prefix", "suffix"]
    sign_style: Literal["leading_minus", "accounting_parentheses"]
    logical = "currency"

    @field_validator("currency_code", mode="before")
    @classmethod
    def _code(cls, v: Any) -> str:
        if not (isinstance(v, str) and re.fullmatch(r"[A-Z]{3}", v)):
            raise violation(
                _P.DECLARED_CAST_CURRENCY_CODE_INVALID,
                "currency_code must be three uppercase ASCII letters.",
            )
        return v

    @field_validator("token", mode="before")
    @classmethod
    def _token(cls, v: Any) -> str:
        if not isinstance(v, str) or not v or len(v.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise violation(
                _P.DECLARED_CAST_CONTRACT_INVALID,
                f"currency token must be 1..{MAX_TOKEN_BYTES} UTF-8 bytes.",
            )
        if (
            not v.isprintable()
            or any(c.isdigit() for c in v)
            or v.strip(" ") == ""
            or "-" in v
            or "(" in v
            or ")" in v
        ):
            raise violation(
                _P.DECLARED_CAST_CONTRACT_INVALID,
                "currency token must not contain digits or signs.",
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def _code_required(cls, data: Any) -> Any:
        if isinstance(data, dict) and "currency_code" not in data:
            raise violation(
                _P.DECLARED_CAST_CURRENCY_CODE_INVALID,
                "currency_code is required (a symbol is not an identity).",
            )
        return data


class Overpunch(_Precision):
    id: Literal["cobol/overpunch-ascii-trailing/v1"]
    logical = "decimal"


class Zoned(_Precision):
    id: Literal["cobol/zoned-ebcdic-trailing/v1"]
    signed: bool = True
    logical = "decimal"
    binary = True


class Comp3(_Precision):
    id: Literal["cobol/comp-3/v1"]
    signed: bool = True
    logical = "decimal"
    binary = True


# -- output-side formats -----------------------------------------------------------------------


class _Padded(ContractModel):
    """Fixed-width output controls. A declared width includes sign, token, separators and pad."""

    width: int | None = None
    width_unit: WidthUnit | None = None
    pad: str = " "
    align: Align = "right"

    @model_validator(mode="before")
    @classmethod
    def _width_declared(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        width, unit = data.get("width"), data.get("width_unit")
        if width is None and cls._width_required:
            raise violation(_P.DECLARED_OUTPUT_WIDTH_UNKNOWN, "output width is required.")
        if width is not None:
            if isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= 1_000_000:
                raise violation(
                    _P.DECLARED_OUTPUT_WIDTH_UNKNOWN, "output width must be a positive integer."
                )
        if (width is not None or unit is not None) and unit not in ("code_points", "bytes"):
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "width_unit must be code_points or bytes."
            )
        return data

    @field_validator("pad", mode="before")
    @classmethod
    def _pad(cls, v: Any) -> str:
        if not (isinstance(v, str) and len(v) == 1 and v.isprintable()):
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "pad must be one printable character."
            )
        return v

    @model_validator(mode="after")
    def _pad_is_one_unit(self) -> _Padded:
        if self.width_unit == "bytes" and len(self.pad.encode("utf-8")) != 1:
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "a byte-width pad must be one byte."
            )
        # Stripping the pad is how the exact-inverse oracle recovers the value; a digit pad
        # would eat significant digits (only integer/zero-pad/v1 pads with 0 and strips nothing).
        if self.pad.isdigit() and getattr(self, "id", "") != "integer/zero-pad/v1":
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "a numeric pad would hide digits."
            )
        return self

    _width_required: ClassVar[bool] = False


class StringExactOut(_Padded, StringExact):
    align: Align = "left"


class IntegerAsciiOut(_Padded, IntegerAscii):
    pass


class IntegerZeroPad(_Padded):
    id: Literal["integer/zero-pad/v1"]
    signed: bool = True
    bit_width: int = 64
    pad: str = "0"
    logical = "integer"
    _width_required: ClassVar[bool] = True

    @field_validator("bit_width", mode="before")
    @classmethod
    def _bits(cls, v: Any) -> int:
        return IntegerAscii._bits(v)

    @model_validator(mode="after")
    def _zero_right(self) -> IntegerZeroPad:
        if self.pad != "0" or self.align != "right":
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED,
                "integer/zero-pad/v1 pads with 0 on the left.",
            )
        return self


class DecimalPlainOut(_Padded, DecimalPlain):
    pass


class DecimalGroupedOut(_Padded, DecimalGrouped):
    pass


class DateIsoOut(_Padded, DateIso):
    @model_validator(mode="after")
    def _static(self) -> DateIsoOut:
        if self.width is not None and self.width < 10:
            raise violation(
                _P.DECLARED_OUTPUT_WIDTH_UNKNOWN, "date/iso8601/v1 renders 10 characters."
            )
        return self


class DateStrftime(_Padded):
    id: Literal["date/strftime/v1"]
    format: str
    logical = "date"

    @field_validator("format", mode="before")
    @classmethod
    def _fmt(cls, v: Any) -> str:
        return DateStrptime._fmt(v)

    @model_validator(mode="after")
    def _static(self) -> DateStrftime:
        if self.width is not None and self.width < date_width(self.format):
            raise violation(
                _P.DECLARED_OUTPUT_WIDTH_UNKNOWN,
                "declared width is narrower than the rendered date.",
            )
        return self


class CurrencyOut(_Padded, Currency):
    pass


# -- registry of shapes (descriptor behaviour is attached in declared_casting._catalog) --------

PARSE_MODELS: dict[str, type[ContractModel]] = {
    m.model_fields["id"].annotation.__args__[0]: m  # type: ignore[union-attr]
    for m in (
        StringExact,
        IntegerAscii,
        DecimalPlain,
        DecimalGrouped,
        DateIso,
        DateStrptime,
        Currency,
        Overpunch,
        Zoned,
        Comp3,
    )
}
OUTPUT_MODELS: dict[str, type[ContractModel]] = {
    m.model_fields["id"].annotation.__args__[0]: m  # type: ignore[union-attr]
    for m in (
        StringExactOut,
        IntegerAsciiOut,
        IntegerZeroPad,
        DecimalPlainOut,
        DecimalGroupedOut,
        DateIsoOut,
        DateStrftime,
        CurrencyOut,
        Overpunch,
        Zoned,
        Comp3,
    )
}


class _UnknownFormat(ContractModel):
    id: str

    @model_validator(mode="before")
    @classmethod
    def _unknown(cls, data: Any) -> Any:
        raise violation(_P.DECLARED_CAST_FORMAT_UNKNOWN, "format id is unknown or retired.")


def _tagger(models: dict[str, type[ContractModel]]):
    def tag(value: Any) -> str:
        ident = value.get("id") if isinstance(value, dict) else getattr(value, "id", None)
        return ident if isinstance(ident, str) and ident in models else "__unknown__"

    return tag


def _union(models: dict[str, type[ContractModel]]) -> Any:
    members = (
        *(Annotated[m, Tag(i)] for i, m in models.items()),
        Annotated[_UnknownFormat, Tag("__unknown__")],
    )
    return Annotated[Union[members], Discriminator(_tagger(models))]  # noqa: UP007


ParseFormat = _union(PARSE_MODELS)
OutputFormat = _union(OUTPUT_MODELS)
_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    "parse": TypeAdapter(ParseFormat),
    "output": TypeAdapter(OutputFormat),
}


def first_code(exc: ValidationError) -> tuple[str, str]:
    """The first registered reason in a pydantic error list (its ``type``) and its message."""
    for err in exc.errors():
        if reasons.is_registered(err["type"]):
            return err["type"], err["msg"]
    return str(_P.DECLARED_CAST_CONTRACT_INVALID), "declared contract is inconsistent."


def validate_format_spec(spec: Any, *, direction: Direction = "parse") -> Any:
    """Validate one parse/output format spec; raise ``DeclaredCastContractError`` with a registered code."""
    if not isinstance(spec, (dict, ContractModel)):
        raise DeclaredCastContractError(
            str(_P.DECLARED_CAST_CONTRACT_INVALID), "format spec must be a mapping."
        )
    try:
        return _ADAPTERS[direction].validate_python(spec)
    except ValidationError as exc:
        code, message = first_code(exc)
        raise DeclaredCastContractError(code, message) from None

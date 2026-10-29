"""The F06 declared parsing, casting and quarantine contract (version 1).

One discriminated, closed contract per mask column: ``logical_type``, ``parse_format``,
``sentinel_to_null``, ``error_policy`` and ``output_format``. Every format is identified by an
exact ``/v1`` id; an unknown id is a registered rejection, never a default. This module is a
leaf: it validates shape and cross-field cohesion and names the reason code of every rejection,
but it executes nothing (parsing and formatting live in ``decoy_engine.declared_casting``).

Pattern sources: PyArrow ``csv.ConvertOptions`` (explicit column types and null-token lists),
Python ``decimal`` (exact base-10 values; precision is bounded to Arrow's ``decimal128`` range
of 1..38 so a declared precision can never become a compute cost), Unicode TR35 (separators,
tokens and symbols vary by locale, so V1 requires every one of them to be declared and ships no
locale profile), and the IBM overpunch / zoned / packed-decimal conventions for the COBOL kinds.
"""

from __future__ import annotations

import itertools
import re
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from decoy_engine import reasons
from decoy_engine.errors import DecoyError

CATALOG_VERSION = 1
MAX_PRECISION = 38
MAX_SENTINELS = 256
MAX_SENTINEL_BYTES = 1024
MAX_TOKEN_BYTES = 32
INTEGER_WIDTHS = (8, 16, 32, 64)

LogicalType = Literal["string", "integer", "decimal", "date", "currency"]
ErrorPolicy = Literal["fail", "null", "quarantine"]
WidthUnit = Literal["code_points", "bytes"]
Align = Literal["left", "right"]
Direction = Literal["parse", "output"]

# Registered reason names by contract failure (the reason registry owns the semantics).
_P = reasons.plan


def violation(code: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(str(code), message)


class DeclaredCastContractError(DecoyError):
    """A declared contract fragment was rejected; ``code`` is a registered plan reason."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    # filled by the catalog: which logical type each format yields and whether it is binary
    logical: ClassVar[str] = ""
    binary: ClassVar[bool] = False


def _strict_int(value: Any, code: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise violation(code, f"{what} must be an integer between {lo} and {hi}.")
    return value


class _Precision(_Model):
    precision: int
    scale: int

    @field_validator("precision", mode="before")
    @classmethod
    def _precision(cls, v: Any) -> int:
        return _strict_int(v, _P.DECLARED_CAST_PRECISION_INVALID, 1, MAX_PRECISION, "precision")

    @field_validator("scale", mode="before")
    @classmethod
    def _scale_type(cls, v: Any) -> int:
        return _strict_int(v, _P.DECLARED_CAST_SCALE_INVALID, 0, 10**6, "scale")

    @model_validator(mode="after")
    def _scale_within_precision(self) -> _Precision:
        if self.scale > self.precision:
            raise violation(_P.DECLARED_CAST_SCALE_INVALID, "scale must not exceed precision.")
        return self


def _separator(value: Any, what: str) -> str:
    ok = (
        isinstance(value, str)
        and len(value) == 1
        and value.isascii()
        and value.isprintable()
        and value not in "0123456789+-() "
    )
    if not ok:
        raise violation(
            _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, f"{what} must be one printable ASCII separator."
        )
    return value  # type: ignore[no-any-return]


class _Separators(_Model):
    radix: str = "."
    group: str | None = None

    @field_validator("radix", mode="before")
    @classmethod
    def _radix(cls, v: Any) -> str:
        return _separator(v, "radix")

    @field_validator("group", mode="before")
    @classmethod
    def _group(cls, v: Any) -> str | None:
        return None if v is None else _separator(v, "group")

    @model_validator(mode="after")
    def _distinct(self) -> _Separators:
        if self.group is not None and self.group == self.radix:
            raise violation(
                _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED, "radix and group separators must differ."
            )
        return self


# -- parse-side formats ------------------------------------------------------------------------


class StringExact(_Model):
    id: Literal["string/exact/v1"]
    logical = "string"


class IntegerAscii(_Model):
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
        return v  # type: ignore[no-any-return]


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


class DateIso(_Model):
    id: Literal["date/iso8601/v1"]
    logical = "date"


class DateStrptime(_Model):
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


class _Padded(_Model):
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
        return IntegerAscii._bits(v)  # type: ignore[no-any-return]

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
        return DateStrptime._fmt(v)  # type: ignore[no-any-return]

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

PARSE_MODELS: dict[str, type[_Model]] = {
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
OUTPUT_MODELS: dict[str, type[_Model]] = {
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


class _UnknownFormat(_Model):
    id: str

    @model_validator(mode="before")
    @classmethod
    def _unknown(cls, data: Any) -> Any:
        raise violation(_P.DECLARED_CAST_FORMAT_UNKNOWN, "format id is unknown or retired.")


def _tagger(models: dict[str, type[_Model]]):
    def tag(value: Any) -> str:
        ident = value.get("id") if isinstance(value, dict) else getattr(value, "id", None)
        return ident if isinstance(ident, str) and ident in models else "__unknown__"

    return tag


def _union(models: dict[str, type[_Model]]) -> Any:
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
    if not isinstance(spec, (dict, _Model)):
        raise DeclaredCastContractError(
            str(_P.DECLARED_CAST_CONTRACT_INVALID), "format spec must be a mapping."
        )
    try:
        return _ADAPTERS[direction].validate_python(spec)
    except ValidationError as exc:
        code, message = first_code(exc)
        raise DeclaredCastContractError(code, message) from None


# -- sentinel ----------------------------------------------------------------------------------


class ExactSentinelSetV1(_Model):
    """Ordered-unique exact tokens matched before parsing. Binary kinds list hex-encoded bytes."""

    id: Literal["sentinel/exact/v1"]
    values: list[str]

    @field_validator("values", mode="before")
    @classmethod
    def _values(cls, v: Any) -> list[str]:
        bad = (
            not isinstance(v, (list, tuple))
            or not 1 <= len(v) <= MAX_SENTINELS
            or any(not isinstance(x, str) for x in v)
            or len(set(v)) != len(v)
        )
        if bad:
            raise violation(
                _P.DECLARED_CAST_SENTINEL_CONFIG_INVALID,
                f"sentinel values must be 1..{MAX_SENTINELS} unique strings.",
            )
        return list(v)

    def decoded(self, *, binary: bool) -> tuple[Any, ...]:
        """Tokens as text, or as bytes for a binary kind; raises when a token is the wrong kind."""
        out: list[Any] = []
        for token in self.values:
            if binary:
                if len(token) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", token):
                    raise violation(
                        _P.DECLARED_CAST_SENTINEL_CONFIG_INVALID,
                        "binary sentinels are hex strings.",
                    )
                raw: Any = bytes.fromhex(token)
            else:
                raw = token
            size = len(raw) if binary else len(token.encode("utf-8", "surrogatepass"))
            if size > MAX_SENTINEL_BYTES:
                raise violation(
                    _P.DECLARED_CAST_SENTINEL_CONFIG_INVALID,
                    f"a sentinel exceeds {MAX_SENTINEL_BYTES} encoded bytes.",
                )
            out.append(raw)
        if binary and len({t.lower() for t in self.values}) != len(self.values):
            raise violation(
                _P.DECLARED_CAST_SENTINEL_CONFIG_INVALID, "sentinel values must be unique."
            )
        return tuple(out)


def validate_sentinel_spec(spec: Any, *, binary: bool) -> ExactSentinelSetV1:
    try:
        model = ExactSentinelSetV1.model_validate(spec)
        model.decoded(binary=binary)
    except ValidationError as exc:
        code, message = first_code(exc)
        raise DeclaredCastContractError(code, message) from None
    except PydanticCustomError as exc:
        raise DeclaredCastContractError(exc.type, exc.message_template) from None
    return model


# -- the per-column contract -------------------------------------------------------------------

_LOGICAL_FOR = {
    "string": {"string/exact/v1"},
    "integer": {"integer/ascii/v1"},
    "decimal": {
        "decimal/plain/v1",
        "decimal/grouped/v1",
        "cobol/overpunch-ascii-trailing/v1",
        "cobol/zoned-ebcdic-trailing/v1",
        "cobol/comp-3/v1",
    },
    "date": {"date/iso8601/v1", "date/strptime/v1"},
    "currency": {"currency/explicit/v1"},
}
_OUTPUT_LOGICAL = {
    "string": {"string/exact/v1"},
    "integer": {"integer/ascii/v1", "integer/zero-pad/v1"},
    "decimal": _LOGICAL_FOR["decimal"],
    "date": {"date/iso8601/v1", "date/strftime/v1"},
    "currency": {"currency/explicit/v1"},
}


class DeclaredColumnContractV1(BaseModel):
    """The whole per-column contract; construction proves the fields agree with each other."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    logical_type: LogicalType
    parse_format: Any
    sentinel_to_null: ExactSentinelSetV1 | None = None
    error_policy: ErrorPolicy = "fail"
    output_format: Any = None

    @field_validator("parse_format", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> Any:
        return _ADAPTERS["parse"].validate_python(v)

    @field_validator("output_format", mode="before")
    @classmethod
    def _output(cls, v: Any) -> Any:
        return None if v is None else _ADAPTERS["output"].validate_python(v)

    @model_validator(mode="after")
    def _cohesive(self) -> DeclaredColumnContractV1:
        if self.parse_format.id not in _LOGICAL_FOR[self.logical_type]:
            raise violation(
                _P.DECLARED_CAST_CONTRACT_INVALID,
                "parse_format does not produce the declared logical_type.",
            )
        if self.output_format is not None:
            if self.output_format.id not in _OUTPUT_LOGICAL[self.logical_type]:
                raise violation(
                    _P.DECLARED_CAST_CONTRACT_INVALID,
                    "output_format does not render the declared logical_type.",
                )
            if (
                self.output_format.logical != self.parse_format.logical
                and self.logical_type != "currency"
            ):
                raise violation(
                    _P.DECLARED_CAST_CONTRACT_INVALID, "output_format changes the logical type."
                )
            self._same_numeric_domain()
        if self.sentinel_to_null is not None:
            try:
                self.sentinel_to_null.decoded(binary=self.parse_format.binary)
            except PydanticCustomError:
                raise
        return self

    def _same_numeric_domain(self) -> None:
        p, o = self.parse_format, self.output_format
        for attr in ("precision", "scale", "currency_code"):
            if hasattr(p, attr) and hasattr(o, attr) and getattr(p, attr) != getattr(o, attr):
                raise violation(
                    _P.DECLARED_CAST_CONTRACT_INVALID,
                    f"output_format {attr} differs from parse_format.",
                )

    @property
    def binary_input(self) -> bool:
        return bool(self.parse_format.binary)

    def to_payload(self) -> dict[str, Any]:
        """Canonical JSON-compatible form (sorted by the serializer, defaults made explicit)."""
        return {
            "logical_type": self.logical_type,
            "parse_format": self.parse_format.model_dump(),
            "sentinel_to_null": None
            if self.sentinel_to_null is None
            else self.sentinel_to_null.model_dump(),
            "error_policy": self.error_policy,
            "output_format": None
            if self.output_format is None
            else self.output_format.model_dump(),
        }


def build_contract(fields: dict[str, Any]) -> DeclaredColumnContractV1:
    """Contract from raw column fields; raises ``DeclaredCastContractError``."""
    try:
        return DeclaredColumnContractV1.model_validate(fields)
    except ValidationError as exc:
        code, message = first_code(exc)
        raise DeclaredCastContractError(code, message) from None


CONTRACT_FIELDS = (
    "logical_type",
    "parse_format",
    "sentinel_to_null",
    "error_policy",
    "output_format",
)


class ProtectedQuarantineConfig(BaseModel):
    """The one top-level protected-quarantine declaration. There is deliberately no path or key."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal[1]
    access_policy: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")
    retention_seconds: int = Field(ge=1, le=31_536_000)
    max_rows: int = Field(ge=1, le=100_000_000)
    max_bytes: int = Field(ge=1, le=1 << 40)
    required_protection: Literal["encrypted"]

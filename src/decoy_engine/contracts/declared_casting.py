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

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from decoy_engine.contracts._declared_base import (  # noqa: F401 -- re-exported contract surface
    _P,
    CATALOG_VERSION,
    INTEGER_WIDTHS,
    MAX_PRECISION,
    MAX_SENTINEL_BYTES,
    MAX_SENTINELS,
    MAX_TOKEN_BYTES,
    Align,
    ContractModel,
    DeclaredCastContractError,
    Direction,
    ErrorPolicy,
    LogicalType,
    WidthUnit,
    violation,
)
from decoy_engine.contracts._declared_formats import *  # noqa: F403 -- the format catalog
from decoy_engine.contracts._declared_formats import (  # noqa: F401
    _ADAPTERS,
    OUTPUT_MODELS,
    PARSE_MODELS,
    date_pattern,
    date_width,
    first_code,
    validate_format_spec,
)

# -- sentinel ----------------------------------------------------------------------------------


class ExactSentinelSetV1(ContractModel):
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

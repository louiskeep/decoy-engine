"""Shared building blocks of the F06 contract: constants, the rejection error and base models.

Leaf of the contract package (imports only the reason registry and pydantic).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
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


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    # filled by the catalog: which logical type each format yields and whether it is binary
    logical: ClassVar[str] = ""
    binary: ClassVar[bool] = False


def _strict_int(value: Any, code: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise violation(code, f"{what} must be an integer between {lo} and {hi}.")
    return value


class _Precision(ContractModel):
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


class _Separators(ContractModel):
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

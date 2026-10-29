"""Config adapters for the F06 declared-casting contract.

The contract itself (shapes, cohesion, reason codes) lives in ``decoy_engine.contracts.
declared_casting``; this module only translates its registered rejections into pydantic custom
errors so ``ValidationError.errors()[i]["type"]`` is the registered reason code.
"""

from __future__ import annotations

from typing import Any

from pydantic_core import PydanticCustomError

from decoy_engine import reasons
from decoy_engine.contracts.declared_casting import (
    CONTRACT_FIELDS,
    DeclaredCastContractError,
    ExactSentinelSetV1,
    build_contract,
    validate_format_spec,
)

_INVALID = str(reasons.plan.DECLARED_CAST_CONTRACT_INVALID)
LOGICAL_TYPES = ("string", "integer", "decimal", "date", "currency")
ERROR_POLICIES = ("fail", "null", "quarantine")


def _raise(exc: DeclaredCastContractError) -> PydanticCustomError:
    return PydanticCustomError(exc.code, exc.message or "declared contract is invalid.")


def typed_format(value: Any, direction: str) -> Any:
    if value is None:
        return None
    try:
        return validate_format_spec(value, direction=direction)  # type: ignore[arg-type]
    except DeclaredCastContractError as exc:
        raise _raise(exc) from None


def typed_sentinel(value: Any) -> Any:
    if value is None or isinstance(value, ExactSentinelSetV1):
        return value
    if not isinstance(value, dict):
        raise PydanticCustomError(
            str(reasons.plan.DECLARED_CAST_SENTINEL_CONFIG_INVALID),
            "sentinel_to_null must be a mapping.",
        )
    try:
        return ExactSentinelSetV1.model_validate(value)
    except Exception as exc:  # pydantic ValidationError carrying a registered type
        errors = getattr(exc, "errors", None)
        for err in errors() if errors else ():
            if reasons.is_registered(err["type"]):
                raise PydanticCustomError(err["type"], err["msg"]) from None
        raise PydanticCustomError(
            str(reasons.plan.DECLARED_CAST_SENTINEL_CONFIG_INVALID), "sentinel_to_null is invalid."
        ) from None


def closed_choice(value: Any, choices: tuple[str, ...], what: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str) or value not in choices:
        raise PydanticCustomError(_INVALID, f"{what} must be one of {', '.join(choices)}.")
    return value


def check_column(column: Any) -> None:
    """Prove a column's five optional fields form one cohesive contract (or are all unset)."""
    present = {k: getattr(column, k) for k in CONTRACT_FIELDS if getattr(column, k) is not None}
    if not present:
        return
    if "logical_type" not in present or "parse_format" not in present:
        raise PydanticCustomError(_INVALID, "logical_type and parse_format are required together.")
    try:
        build_contract({**present, "error_policy": present.get("error_policy", "fail")})
    except DeclaredCastContractError as exc:
        raise _raise(exc) from None
    if column.error_policy is None:
        column.error_policy = "fail"


def require_protected_block(config: Any) -> None:
    """A column that quarantines needs the one top-level protected-quarantine declaration."""
    if config.protected_quarantine is not None:
        return
    for table in config.tables:
        for col in getattr(table, "columns", None) or ():
            if getattr(col, "error_policy", None) == "quarantine":
                raise PydanticCustomError(
                    _INVALID, "error_policy quarantine requires a protected_quarantine block."
                )


def recover_serialization_schema(core_schema: Any, handler: Any) -> Any:
    """Field-enumerating JSON schema for a model whose wrap serializer hides it.

    A wrap serializer makes pydantic report the serialization schema as a generic object. The
    serializer sits on the model schema, which `after` validators wrap, so it is stripped down the
    whole wrapper chain before asking the handler again.
    """
    schema = handler(core_schema)
    if "properties" in schema or "$ref" in schema:
        return schema
    stripped = {k: v for k, v in core_schema.items() if k != "serialization"}
    inner = stripped.get("schema")
    while isinstance(inner, dict):
        inner = {k: v for k, v in inner.items() if k != "serialization"}
        stripped = {**stripped, "schema": inner}
        inner = inner.get("schema")
    return handler(stripped)

"""Pure, config-only compile of the F06 declared-casting contracts.

`compile_declared_casting_contract(config)` is the one validator: `build_run_context` calls it
before the source is profiled (so a config-only failure never reads data) and `compile_plan`
calls it again and stamps the result, which is deterministic and side-effect free. It proves each
declared column's contract is cohesive, that the masking strategy can produce what the output
formatter consumes, that fixed-width layouts agree with the declaration, and that relationship
endpoints share one equality domain. It never touches data and never mutates `config`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from decoy_engine import reasons
from decoy_engine.contracts import declared_casting as C  # noqa: N812 -- short alias for the contract namespace
from decoy_engine.declared_casting import _catalog
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import DeclaredCastingPlan, PlanDeclaredColumn, freeze_json

_P = reasons.plan
_DTYPE_FAMILY = {
    "integer": ("int", "uint"),
    "decimal": ("decimal",),
    "currency": ("decimal",),
    "date": ("date",),
    "string": ("string", "str", "object", "utf8", "large_string"),
}
# Strategies whose output type is proven for a declared column. Anything else is "unproven" and
# is rejected for a non-string logical type, so a typed formatter never re-parses a guess.
_STRING_ONLY_OUT = "string"


def strategy_output_logical(
    strategy: str, provider_config: Mapping[str, Any], logical: str
) -> str | None:
    """The logical type a masking strategy returns for a declared column, or None if unproven."""
    if strategy == "passthrough":
        return logical
    if strategy == "hash":
        return _STRING_ONLY_OUT
    if strategy == "bucketize":
        if logical != "integer":
            return None
        fmt = str(provider_config.get("format", "lower")).lower()
        width = provider_config.get("width")
        integral = isinstance(width, int) and not isinstance(width, bool)
        if fmt == "lower" and integral:
            return "integer"
        return _STRING_ONLY_OUT if fmt == "range" and integral else None
    if strategy == "date_shift":
        return "date" if logical == "date" else None
    return _STRING_ONLY_OUT if logical == "string" else None


def equality_domain(contract: C.DeclaredColumnContractV1) -> tuple[Any, ...]:
    """Values compare equal across lexical formats only within the same canonical domain."""
    p = contract.parse_format
    if contract.logical_type == "integer":
        return ("integer", p.signed, p.bit_width)
    if contract.logical_type in ("decimal", "currency"):
        return (contract.logical_type, p.precision, p.scale, getattr(p, "currency_code", None))
    return (contract.logical_type,)


def _fail(code: Any, path: str, message: str) -> PlanCompileError:
    return PlanCompileError(code=str(code), path=path, message=message)


def _declared_fields(col: Mapping[str, Any]) -> dict[str, Any]:
    return {k: col[k] for k in C.CONTRACT_FIELDS if col.get(k) is not None}


def _layout_columns(layout: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    return {c["name"]: c for c in (layout or {}).get("columns", ())}


def _check_source(table: str, col_name: str, source: Mapping[str, Any] | None) -> None:
    if not source or source.get("format") != "fixed_width":
        return
    layout = source.get("layout") or {}
    path = f"tables.{table}.columns.{col_name}"
    if not layout.get("exact_record_width"):
        raise _fail(
            _P.DECLARED_CAST_CONTRACT_INVALID,
            path,
            "A fixed-width source feeding a declared column must set layout.exact_record_width.",
        )
    lay = _layout_columns(layout).get(col_name)
    if lay is None or lay.get("type", "str") != "str":
        raise _fail(
            _P.DECLARED_CAST_CONTRACT_INVALID,
            path,
            "A declared column must read its fixed-width field as str so the lexeme survives.",
        )


def _check_target(
    table: str,
    col_name: str,
    contract: C.DeclaredColumnContractV1,
    target: Mapping[str, Any] | None,
) -> None:
    if not target or target.get("format") != "fixed_width":
        return
    path = f"tables.{table}.columns.{col_name}"
    out = contract.output_format
    if out is None:
        raise _fail(
            _P.DECLARED_OUTPUT_WIDTH_UNKNOWN,
            path,
            "A fixed-width target needs an explicit output_format.",
        )
    width = _catalog.output_width(out)
    lay = _layout_columns(target.get("layout")).get(col_name)
    if width is None or lay is None or lay.get("width") != width:
        raise _fail(
            _P.DECLARED_OUTPUT_WIDTH_UNKNOWN,
            path,
            "The output width is not provably equal to the target layout width.",
        )
    if getattr(out, "width_unit", "code_points") == "bytes" and not out.binary:
        raise _fail(
            _P.DECLARED_CAST_DIRECTIVE_UNSUPPORTED,
            path,
            "The fixed-width writer measures code points; a byte width is not admitted.",
        )


def compile_declared_casting_contract(config: Mapping[str, Any]) -> DeclaredCastingPlan | None:
    """Validate and freeze every declared column, or return None when there are none."""
    columns: list[PlanDeclaredColumn] = []
    contracts: dict[tuple[str, str], C.DeclaredColumnContractV1] = {}
    needs_quarantine = False
    sources, targets = config.get("sources") or {}, config.get("targets") or {}
    for table in config.get("tables") or ():
        tname = table.get("name", "?")
        for gen in table.get("generate_columns") or ():
            if any(gen.get(k) is not None for k in C.CONTRACT_FIELDS):
                raise _fail(
                    _P.DECLARED_CAST_CONTRACT_INVALID,
                    f"tables.{tname}",
                    "Generation columns cannot declare parsing.",
                )
        for col in table.get("columns") or ():
            fields = _declared_fields(col)
            if not fields:
                continue
            cname = col.get("name", "?")
            path = f"tables.{tname}.columns.{cname}"
            try:
                contract = C.build_contract(fields)
            except C.DeclaredCastContractError as exc:
                raise _fail(exc.code, path, exc.message) from None
            _check_column(path, col, contract)
            _check_source(tname, cname, sources.get(tname))
            _check_target(tname, cname, contract, targets.get(tname))
            needs_quarantine |= contract.error_policy == "quarantine"
            contracts[(tname, cname)] = contract
            columns.append(
                PlanDeclaredColumn(
                    table=tname,
                    column=cname,
                    strategy=col.get("strategy", ""),
                    contract=freeze_json(contract.to_payload()),
                )
            )
    if not columns:
        return None
    quarantine = config.get("protected_quarantine")
    if needs_quarantine and not quarantine:
        raise _fail(
            _P.DECLARED_CAST_CONTRACT_INVALID,
            "protected_quarantine",
            "error_policy quarantine requires a protected_quarantine block.",
        )
    _check_relationships(config, contracts)
    columns.sort(key=lambda c: (c.table, c.column))
    return DeclaredCastingPlan(
        catalog_version=C.CATALOG_VERSION,
        columns=tuple(columns),
        quarantine=freeze_json(dict(quarantine)) if quarantine else None,
    )


def _check_column(path: str, col: Mapping[str, Any], contract: C.DeclaredColumnContractV1) -> None:
    dtype = col.get("dtype")
    if dtype is not None:
        families = _DTYPE_FAMILY[contract.logical_type]
        if not str(dtype).lower().startswith(families):
            raise _fail(
                _P.DECLARED_TYPE_STRATEGY_INCOMPATIBLE,
                path,
                "dtype conflicts with the declared logical_type.",
            )
    strategy = col.get("strategy", "")
    provider_config = col.get("provider_config") or {}
    if strategy == "date_shift" and provider_config.get("date_format"):
        raise _fail(
            _P.DECLARED_CAST_CONTRACT_INVALID,
            path,
            "date_shift.date_format and a declared parse_format conflict; declare the format once.",
        )
    produced = strategy_output_logical(strategy, provider_config, contract.logical_type)
    if produced is None:
        raise _fail(
            _P.DECLARED_TYPE_STRATEGY_INCOMPATIBLE,
            path,
            "The strategy's output type is not proven for this declared column.",
        )
    out = contract.output_format
    if (
        out is not None
        and out.logical != produced
        and not (produced == "currency" and out.logical == "currency")
    ):
        raise _fail(
            _P.DECLARED_TYPE_STRATEGY_INCOMPATIBLE,
            path,
            "The output_format cannot render the strategy's output.",
        )


def _check_relationships(
    config: Mapping[str, Any], contracts: Mapping[tuple[str, str], C.DeclaredColumnContractV1]
) -> None:
    for rel in config.get("relationships") or ():
        parent, children = rel.get("parent") or {}, rel.get("children") or ()
        pairs = [
            ((parent.get("table"), pc), (child.get("table"), cc))
            for child in children
            for pc, cc in zip(parent.get("columns", ()), child.get("columns", ()), strict=False)
        ]
        for left, right in pairs:
            a, b = contracts.get(left), contracts.get(right)
            if a is None and b is None:
                continue
            if a is None or b is None or equality_domain(a) != equality_domain(b):
                raise _fail(
                    _P.DECLARED_TYPE_STRATEGY_INCOMPATIBLE,
                    f"relationships.{left[0]}.{left[1]}",
                    "Relationship endpoints must share one declared equality domain.",
                )

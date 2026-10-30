"""The one route/target/source capability decision for declared contracts.

`evaluate_declared_casting` never reads data. Every production route owner calls it (or
`declared_route_guard`, which wraps it); a sentry inventories the owners, so a new route cannot
quietly apply a contract differently. Routes declare CAPABILITIES here (what they can do), never
format names: which formats exist is the catalog's business, so there is no second allowlist.
A truthful registered refusal is the correct answer for an unsupported combination; the engine
never builds a route-private parser to gain admission.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.contracts import (
    declared_casting as C,  # noqa: N812 -- short alias for the contract namespace
)
from decoy_engine.declared_casting import _catalog
from decoy_engine.reasons import ReasonCode

ROUTES = ("full_frame", "sequential", "chunked", "out_of_core", "mixed", "unified_slice")

# What each route can do for a declared column. Only the full-frame route has the pre-mask and
# post-mask boundary wired in; the others lack a bounded-memory cast/format/quarantine hook, so
# they decline rather than approximate (a registered refusal, per the F06 plan).
_FULL = frozenset(
    {
        "pre_mask_cast",
        "post_mask_format",
        "issue_counts",
        "binary_cells",
        "cast_before_join",
        "row_quarantine",
    }
)
ROUTE_CAPABILITIES: Mapping[str, frozenset[str]] = {
    "full_frame": _FULL,
    "sequential": frozenset(),
    "chunked": frozenset(),
    "out_of_core": frozenset(),
    "mixed": frozenset(),
    "unified_slice": frozenset(),
}
BYTE_PRESERVING_TARGETS = frozenset({"parquet", "memory", None})


@dataclass(frozen=True)
class CastRouteDecision:
    admitted: bool
    rejection: ReasonCode | None
    requirements: tuple[str, ...]
    missing: tuple[str, ...]
    widths: Mapping[str, int | None]

    def output_width(self, format_id: str) -> int | None:
        return self.widths.get(format_id)


def _columns(source: Any) -> tuple[Any, ...]:
    """Declared columns of a Plan, a DeclaredCastingPlan or a plain sequence; () for anything
    else (test doubles and pre-F06 plans carry none)."""
    if source is None:
        return ()
    if isinstance(source, (list, tuple)):
        return tuple(source)
    block = getattr(source, "declared_casting", source)
    columns = getattr(block, "columns", None) if block is not None else None
    return tuple(columns) if columns is not None else ()


def has_contracts(plan: Any) -> bool:
    return bool(_columns(plan))


def contract_model(column: Any) -> C.DeclaredColumnContractV1:
    from decoy_engine.plan._types import unfreeze_json

    return C.build_contract(unfreeze_json(column.contract))


def _requirements(
    columns: Iterable[Any], models: list[C.DeclaredColumnContractV1], plan: Any
) -> set[str]:
    need = {"pre_mask_cast", "issue_counts"}
    for model in models:
        if model.output_format is not None:
            need.add("post_mask_format")
        if model.binary_input:
            need.add("binary_cells")
        if model.error_policy == "quarantine":
            need.add("row_quarantine")
    if getattr(plan, "relationships", None) or (
        isinstance(plan, Mapping) and plan.get("relationships")
    ):
        need.add("cast_before_join")
    return need


def evaluate_declared_casting(
    *,
    route: str,
    plan_contracts: Any,
    source_facts: Mapping[str, Any] | None = None,
    target_facts: Mapping[str, Any] | None = None,
    capabilities: Any = None,
    has_relationships: bool = False,
) -> CastRouteDecision:
    columns = _columns(plan_contracts)
    if not columns:
        return CastRouteDecision(True, None, (), (), {})
    models = [contract_model(c) for c in columns]
    need = _requirements(columns, models, None)
    if has_relationships:
        need.add("cast_before_join")
    have = ROUTE_CAPABILITIES.get(route, frozenset())
    fixed_target = (target_facts or {}).get("kind") == "fixed_width"
    widths: dict[str, int | None] = {}
    for model in models:
        out = model.output_format
        if out is not None:
            widths[out.id] = _catalog.output_width(out)
    rejection: ReasonCode | None = None
    missing: tuple[str, ...] = ()
    if fixed_target and any(
        m.output_format is None or widths.get(m.output_format.id) is None for m in models
    ):
        rejection = reasons.plan.DECLARED_OUTPUT_WIDTH_UNKNOWN
    elif not need <= have:
        rejection, missing = (
            reasons.route.DECLARED_CAST_ROUTE_UNSUPPORTED,
            tuple(sorted(need - have)),
        )
    elif (target_facts or {}).get("kind") not in BYTE_PRESERVING_TARGETS and any(
        m.output_format is not None and m.output_format.binary for m in models
    ):
        rejection = reasons.route.DECLARED_CAST_ROUTE_UNSUPPORTED
        missing = ("byte_preserving_target",)
    else:
        bad = _input_type_mismatch(columns, models, source_facts)
        if bad:
            rejection = reasons.route.DECLARED_CAST_INPUT_TYPE_INCOMPATIBLE
    if (
        rejection is None
        and "row_quarantine" in need
        and not getattr(capabilities, "quarantine_sink", False)
    ):
        rejection, missing = reasons.route.PROTECTED_QUARANTINE_UNAVAILABLE, ("quarantine_sink",)
    return CastRouteDecision(rejection is None, rejection, tuple(sorted(need)), missing, widths)


def _input_type_mismatch(columns: Any, models: Any, source_facts: Mapping[str, Any] | None) -> bool:
    types = (source_facts or {}).get("column_types") or {}
    for col, model in zip(columns, models, strict=True):
        dtype = types.get((col.table, col.column))
        if dtype is None:
            continue
        if model.binary_input:
            ok = (
                pa.types.is_binary(dtype)
                or pa.types.is_large_binary(dtype)
                or pa.types.is_fixed_size_binary(dtype)
            )
        else:
            ok = pa.types.is_string(dtype) or pa.types.is_large_string(dtype)
        if not ok:
            return True
    return False


@dataclass(frozen=True)
class RuntimeCapabilities:
    """What the caller supplied at run time (not knowable from the plan)."""

    quarantine_sink: bool = False


def declared_route_guard(
    route: str,
    *,
    plan: Any = None,
    config: Mapping[str, Any] | None = None,
    raise_on_decline: bool = False,
    source_facts: Mapping[str, Any] | None = None,
    target_facts: Mapping[str, Any] | None = None,
    capabilities: Any = None,
) -> CastRouteDecision:
    """Route owners call this once. A declining admission predicate returns the decision and
    declines in its own way; an executor passes ``raise_on_decline`` for a registered refusal."""
    contracts = _columns(plan) or _columns_from_config(config)
    decision = evaluate_declared_casting(
        route=route,
        plan_contracts=contracts,
        source_facts=source_facts,
        target_facts=target_facts,
        capabilities=capabilities
        if capabilities is not None
        else RuntimeCapabilities(quarantine_sink=True),
        has_relationships=bool((config or {}).get("relationships")),
    )
    if raise_on_decline and not decision.admitted:
        from decoy_engine.execution._errors import DeclaredCastRouteError

        raise DeclaredCastRouteError(
            code=str(decision.rejection),
            message=f"route {route!r} declined: {', '.join(decision.missing) or 'contract'}",
        )
    return decision


def declared_rejection(route: str, *, plan: Any = None) -> str | None:
    """The registered rejection code when ``route`` cannot run ``plan``'s declared contracts."""
    decision = declared_route_guard(route, plan=plan)
    return None if decision.admitted else str(decision.rejection)


def _columns_from_config(config: Mapping[str, Any] | None) -> tuple[Any, ...]:
    if not config:
        return ()
    from decoy_engine.plan._checks_declared_casting import compile_declared_casting_contract

    block = compile_declared_casting_contract(config)
    return tuple(block.columns) if block else ()


def preflight_declared_casting(
    plan: Any,
    config: Mapping[str, Any],
    *,
    execution_mode: str,
    auto_chunk: bool,
    unified_slice_enabled: bool,
    quarantine_runtime: Any,
) -> tuple[str, bool, bool]:
    """Config-only admission, run before the source is profiled or read.

    Returns the ``(execution_mode, auto_chunk, unified_slice_enabled)`` the job must run with: a
    declared job takes the one route that applies the boundary. An explicitly requested route
    without the capability, or a quarantine policy without a qualified sink, is a registered
    refusal here, never a failure discovered after data was read.
    """
    from decoy_engine.declared_casting import (
        _quarantine_protocol as P,  # noqa: N812 -- short alias for the contract namespace
    )
    from decoy_engine.execution._errors import DeclaredCastRouteError

    columns = _columns(plan)
    if not columns:
        return execution_mode, auto_chunk, unified_slice_enabled
    qualified = False
    if plan.quarantine is not None:
        why = P.qualify_runtime(quarantine_runtime, dict(plan.quarantine))
        qualified = why is None
        if not qualified and any(contract_model(c).error_policy == "quarantine" for c in columns):
            raise DeclaredCastRouteError(
                code=reasons.route.PROTECTED_QUARANTINE_UNAVAILABLE,
                message=f"no qualified protected quarantine store ({why}).",
            )
    route = "full_frame" if execution_mode in ("auto", "full_frame") else execution_mode
    targets = config.get("targets") or {}
    for table in sorted({c.table for c in columns}):
        evaluate = evaluate_declared_casting(
            route=route,
            plan_contracts=[c for c in columns if c.table == table],
            target_facts={"kind": (targets.get(table) or {}).get("format")},
            capabilities=RuntimeCapabilities(quarantine_sink=qualified),
            has_relationships=bool(config.get("relationships")),
        )
        if not evaluate.admitted:
            raise DeclaredCastRouteError(
                code=str(evaluate.rejection),
                message=f"route {route!r} cannot apply the declared contract: {', '.join(evaluate.missing) or 'see code'}.",
            )
    return "full_frame", False, False

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

# What each route can do for a declared column. Each route lists only what its own cast/format
# hooks honour; a combination outside the set is a registered refusal, never an approximation.
#   cast_before_join          a declared column is a relationship endpoint: both sides must be
#                             typed before keys are compared or mapped.
#   row_quarantine            a parse failure removes the row before masking.
#   post_mask_row_quarantine  a format failure removes the row after masking.
# `post_mask_parent_row_quarantine` (the same removal on a relationship parent, whose children
# were already mapped against the dropped row) is listed by no route: none re-applies the orphan
# policy to the children after publication drops rows, so it is a registered refusal.
_FULL = frozenset(
    {
        "pre_mask_cast",
        "post_mask_format",
        "issue_counts",
        "binary_cells",
        "row_quarantine",
        "post_mask_row_quarantine",
        "cast_before_join",
    }
)
_BASIC = frozenset({"pre_mask_cast", "post_mask_format", "issue_counts", "binary_cells"})
ROUTE_CAPABILITIES: Mapping[str, frozenset[str]] = {
    "full_frame": _FULL,
    "sequential": _FULL,
    "unified_slice": _FULL - {"cast_before_join"},
    "chunked": _FULL - {"cast_before_join"},
    # The mixed route threads a pre-resolved key column positionally beside the stream, so
    # removing rows would desync it; the out-of-core route publishes rows its parent-key
    # relations were already built from.
    "mixed": _BASIC,
    "out_of_core": _BASIC,
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


def _edges(source: Any) -> Any:
    if source is None:
        return ()
    if isinstance(source, Mapping):
        return source.get("relationships") or ()
    if isinstance(source, (list, tuple)):
        return source
    return getattr(source, "relationships", None) or ()


def relationship_parent_tables(source: Any) -> frozenset[str]:
    """Tables that are the parent end of any relationship (same sources as the endpoints)."""
    out: set[str] = set()
    for edge in _edges(source):
        parent = edge.get("parent") if isinstance(edge, Mapping) else getattr(edge, "parent", None)
        if parent is None:
            continue
        table = parent.get("table") if isinstance(parent, Mapping) else parent.table
        out.add(str(table))
    return frozenset(out)


def relationship_endpoints(source: Any) -> frozenset[tuple[str, str]]:
    """``(table, column)`` ends of every relationship, from a Plan, a config mapping or the
    config's relationship list itself."""
    out: set[tuple[str, str]] = set()
    for edge in _edges(source):
        get = edge.get if isinstance(edge, Mapping) else lambda k, _e=edge: getattr(_e, k, None)
        ends = [get("parent"), *(get("children") or ())]
        for end in ends:
            if end is None:
                continue
            table = end.get("table") if isinstance(end, Mapping) else end.table
            cols = end.get("columns") if isinstance(end, Mapping) else end.columns
            out.update((str(table), str(c)) for c in cols or ())
    return frozenset(out)


def _requirements(
    columns: Iterable[Any],
    models: list[C.DeclaredColumnContractV1],
    endpoints: frozenset[tuple[str, str]],
    parents: frozenset[str] = frozenset(),
) -> set[str]:
    need = {"pre_mask_cast", "issue_counts"}
    for column, model in zip(columns, models, strict=True):
        if model.output_format is not None:
            need.add("post_mask_format")
        if model.binary_input:
            need.add("binary_cells")
        if model.error_policy == "quarantine":
            need.add("row_quarantine")
            if model.output_format is not None:
                need.add("post_mask_row_quarantine")
                if column.table in parents:
                    need.add("post_mask_parent_row_quarantine")
        if (column.table, column.column) in endpoints:
            need.add("cast_before_join")
    return need


def evaluate_declared_casting(
    *,
    route: str,
    plan_contracts: Any,
    source_facts: Mapping[str, Any] | None = None,
    target_facts: Mapping[str, Any] | None = None,
    capabilities: Any = None,
    relationships: Any = None,
) -> CastRouteDecision:
    columns = _columns(plan_contracts)
    if not columns:
        return CastRouteDecision(True, None, (), (), {})
    models = [contract_model(c) for c in columns]
    need = _requirements(
        columns,
        models,
        relationship_endpoints(relationships),
        relationship_parent_tables(relationships),
    )
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
        relationships=(config or {}).get("relationships") or plan,
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
    quarantine_runtime: Any,
) -> None:
    """Config-only admission, run before the source is profiled or read.

    Nothing is rewritten: ``auto`` leaves route choice to the router, whose admission predicates
    each consult :func:`declared_route_guard`. An explicitly requested route that lacks a needed
    capability, or a quarantine policy without a qualified sink, is a registered refusal here,
    never a failure discovered after data was read.
    """
    from decoy_engine.declared_casting import (
        _quarantine_protocol as P,  # noqa: N812 -- short alias for the contract namespace
    )
    from decoy_engine.execution._errors import DeclaredCastRouteError

    columns = _columns(plan)
    if not columns:
        return
    qualified = False
    if plan.quarantine is not None and any(
        contract_model(c).error_policy == "quarantine" for c in columns
    ):
        why = P.qualify_runtime(quarantine_runtime, dict(plan.quarantine))
        qualified = why is None
        if not qualified:
            raise DeclaredCastRouteError(
                code=reasons.route.PROTECTED_QUARANTINE_UNAVAILABLE,
                message=f"no qualified protected quarantine store ({why}).",
            )
    if execution_mode == "auto":
        _refuse_unprovided(columns, config)
        return
    targets = config.get("targets") or {}
    for table in sorted({c.table for c in columns}):
        decision = evaluate_declared_casting(
            route=execution_mode,
            plan_contracts=[c for c in columns if c.table == table],
            target_facts={"kind": (targets.get(table) or {}).get("format")},
            capabilities=RuntimeCapabilities(quarantine_sink=qualified),
            relationships=config.get("relationships"),
        )
        if not decision.admitted:
            raise DeclaredCastRouteError(
                code=str(decision.rejection),
                message=f"route {execution_mode!r} cannot apply the declared contract: {', '.join(decision.missing) or 'see code'}.",
            )


def _refuse_unprovided(columns: tuple[Any, ...], config: Mapping[str, Any]) -> None:
    """On ``auto`` no route can be chosen for a requirement that no route provides."""
    from decoy_engine.execution._errors import DeclaredCastRouteError

    provided = frozenset().union(*ROUTE_CAPABILITIES.values())
    models = [contract_model(c) for c in columns]
    need = _requirements(
        columns,
        models,
        relationship_endpoints(config.get("relationships")),
        relationship_parent_tables(config.get("relationships")),
    )
    if need - provided:
        raise DeclaredCastRouteError(
            code=reasons.route.DECLARED_CAST_ROUTE_UNSUPPORTED,
            message=f"no route can apply the declared contract: {', '.join(sorted(need - provided))}.",
        )


def chunk_stability(config: Mapping[str, Any], table: str) -> tuple[frozenset[str], frozenset[str]]:
    """``(stable, unstable)`` declared columns of ``table`` for a route that masks chunk by chunk.

    The planner reads raw source types, but a declared column leaves the casting boundary as its
    declared type. Integers (kept exact through pandas), dates and strings keep one type on every
    chunk, and an ``output_format`` renders to text or bytes whatever the masker returned. A
    decimal or currency with no ``output_format`` is rebuilt from Python ``Decimal`` objects per
    chunk, so its Arrow precision can differ between chunks: such a table is not chunk-stable.
    """
    stable: set[str] = set()
    unstable: set[str] = set()
    for entry in config.get("tables") or ():
        if not isinstance(entry, Mapping) or entry.get("name") != table:
            continue
        for col in entry.get("columns") or ():
            fields = {k: col[k] for k in C.CONTRACT_FIELDS if col.get(k) is not None}
            if not fields:
                continue
            model = C.build_contract(fields)
            rendered = model.output_format is not None
            if rendered or model.logical_type in ("integer", "date", "string"):
                stable.add(col["name"])
            else:
                unstable.add(col["name"])
    return frozenset(stable), frozenset(unstable)

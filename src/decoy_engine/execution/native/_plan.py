"""NativeExecutionPlan compiler + the public native-route eligibility query.

Task 0.2b. The architecture (Part A.1) names ``NativeExecutionPlan`` as the
backend-neutral description Python compiles before opening any DuckDB/Arrow
resource: per-node input projections, per-node execution requirements, the
per-output Arrow schema, the determinism draw family + key source per node, the
required prepasses / state tables, the diagnostic reducers, and the per-node
fallback policy. This module produces that object and attaches the resolved
requirements to each ``WorkNode`` inertly (nothing routes on them in Phase 0).

``native_route_eligibility`` is the public compatibility query the platform's
``classify_streaming_eligibility`` consults instead of hard-coding a strategy
set. It is config-only (it is handed a config and a table name, not a profile),
so it reads ``StrategyCapabilities`` per column rather than recompiling: a
strategy is native-eligible when its output type is static AND it is row-local
AND it needs no whole-column pass or durable global row number. Every miss is a
coded rejection, so the platform gets machine-readable reasons and the set can
never silently drift from the live strategy registry.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pyarrow as pa

from decoy_engine.execution._runner import WorkNode, build_work_list
from decoy_engine.execution.native._capabilities import (
    StrategyCapabilities,
    capabilities_for,
)
from decoy_engine.execution.native._requirements import (
    NodeRequirements,
    requirements_for,
)
from decoy_engine.plan import compile_plan
from decoy_engine.providers_v2 import get_default_registry


@dataclass(frozen=True)
class NativePlanNode:
    """One node of a compiled ``NativeExecutionPlan``.

    Carries the static capabilities and the resolved requirements together with
    the derived planning fields the executor reads (input projection, output
    schema, determinism family + key source, fallback policy).
    """

    table: str
    columns: tuple[str, ...]
    kind: str
    strategy: str
    capabilities: StrategyCapabilities
    requirements: NodeRequirements
    input_projection: tuple[str, ...]
    output_schema: pa.Schema | None
    draw_family: str | None
    key_source: str | None
    fallback_policy: str


@dataclass(frozen=True)
class NativeExecutionPlan:
    """Backend-neutral description of how a job runs on the native route.

    ``work_nodes`` is the original work list with ``NodeRequirements`` attached
    (inert: no execution path reads the field). ``nodes`` is the enriched native
    view. The parity-oracle route for a held node is expressed by its
    ``fallback_policy`` (``python_only`` sends it to the pinned pandas oracle).
    """

    engine_version: str
    nodes: tuple[NativePlanNode, ...]
    work_nodes: tuple[WorkNode, ...]

    def output_schema_for(self, table: str) -> pa.Schema | None:
        """Merge the per-node output schemas for ``table`` into one schema.

        Returns None when any node in the table has an indeterminate output
        type (the table is then excluded from the native route).
        """
        fields: list[pa.Field] = []
        seen: set[str] = set()
        found = False
        for node in self.nodes:
            if node.table != table:
                continue
            found = True
            if node.output_schema is None:
                return None
            for f in node.output_schema:
                if f.name not in seen:
                    seen.add(f.name)
                    fields.append(f)
        return pa.schema(fields) if found else None


@dataclass(frozen=True)
class NativeEligibility:
    """Result of the public native-route compatibility query.

    ``accepted`` is True only when every column of the table is native-eligible.
    ``rejections`` is a tuple of coded reasons (empty on accept), each naming the
    column and the reason so the platform can surface why a job falls back.
    """

    accepted: bool
    rejections: tuple[str, ...]


def compile_native_plan(
    config: dict[str, Any], profile: Any, *, engine_version: str
) -> NativeExecutionPlan:
    """Compile (config, profile) into a ``NativeExecutionPlan``.

    Runs the existing plan compilation, builds the work list, resolves each
    node's requirements, and attaches them to the work node (inertly) and to the
    enriched native node.
    """
    plan = compile_plan(config, profile, decoy_engine_version=engine_version)
    registry = get_default_registry()
    work = build_work_list(plan, registry)

    attached: list[WorkNode] = []
    nodes: list[NativePlanNode] = []
    for wn in work:
        strategy_name = _resolved_strategy(wn)
        caps = capabilities_for(strategy_name)
        req = requirements_for(wn, plan=plan, profile=profile)
        attached.append(replace(wn, requirements=req))
        nodes.append(
            NativePlanNode(
                table=wn.table,
                columns=wn.columns,
                kind=wn.kind,
                strategy=wn.strategy,
                capabilities=caps,
                requirements=req,
                input_projection=req.required_input_columns,
                output_schema=req.output_arrow_schema,
                draw_family=caps.draw_family,
                key_source=caps.key_source,
                fallback_policy=req.fallback_policy,
            )
        )
    return NativeExecutionPlan(
        engine_version=engine_version,
        nodes=tuple(nodes),
        work_nodes=tuple(attached),
    )


def _resolved_strategy(node: WorkNode) -> str:
    if node.kind == "composite":
        return "<composite>"
    if node.kind == "composite_fk_group":
        return "<group>"
    return node.strategy


def _provider_is_composite(provider: str | None, registry: Any) -> bool:
    """Whether a column's provider fans out to a multi-column composite node.

    This is the EXACT predicate ``build_work_list`` uses to set
    ``WorkNode.kind == "composite"`` (``_runner.py``: a registry-bound provider
    whose capability ``backend_type == "composite"``). ``native_route_eligibility``
    and ``compile_native_plan`` share it so the two public APIs cannot disagree
    on whether a column resolves to a composite node.
    """
    return (
        bool(provider)
        and registry.has(provider)
        and (registry.get_capabilities(provider).backend_type == "composite")
    )


def _column_strategy_key(strategy: str, provider: str | None, registry: Any) -> str:
    """The capabilities key a config column resolves to.

    Mirrors ``build_work_list``: a composite provider fans out to a
    ``<composite>`` node regardless of the column's declared strategy string;
    every other column keys on its scalar strategy.
    """
    if _provider_is_composite(provider, registry):
        return "<composite>"
    return strategy


def native_route_eligibility(config: dict[str, Any], *, table: str) -> NativeEligibility:
    """Report whether ``table`` in ``config`` can run on the native route.

    Config-only (no profile, no recompile), but PROVIDER-AWARE: each mask
    column's (strategy, provider) resolves through ``_column_strategy_key`` to
    the same capabilities key ``compile_native_plan`` derives from
    ``WorkNode.kind``, so a composite-provider column is excluded here exactly as
    the WorkNode path excludes it. A column is native-eligible when its resolved
    output type is static, it is row-local, and it needs no whole-column pass or
    durable global row number. Generation columns are not on the native mask
    route yet. Every miss is a coded rejection.
    """
    table_cfg = _find_table(config, table)
    if table_cfg is None:
        # A table absent from the config has no node to reject.
        return NativeEligibility(accepted=True, rejections=())

    registry = get_default_registry()
    rejections: list[str] = []

    if table_cfg.get("generate_columns"):
        rejections.append("generation_not_native_route:generate_columns")

    for col in table_cfg.get("columns", ()):
        name = col.get("name", "?")
        strategy = col.get("strategy")
        if not strategy:
            rejections.append(f"missing_strategy:{name}")
            continue
        provider = col.get("provider")
        resolved = _column_strategy_key(strategy, provider, registry)
        if resolved == "<composite>":
            # Matches the WorkNode path: a composite provider fans out to a
            # multi-column node with an indeterminate per-column output type.
            rejections.append(f"composite_provider_multi_column:{name}:{provider}")
            continue
        try:
            caps = capabilities_for(resolved)
        except KeyError:
            rejections.append(f"unclassified_strategy:{name}:{resolved}")
            continue
        reason = _native_rejection(name, resolved, caps)
        if reason is not None:
            rejections.append(reason)

    return NativeEligibility(accepted=not rejections, rejections=tuple(rejections))


def _native_rejection(name: str, strategy: str, caps: StrategyCapabilities) -> str | None:
    """The coded reason ``name`` cannot run natively, or None when it can."""
    if not caps.output_type_is_static:
        return f"output_type_indeterminate:{name}:{strategy}"
    if not caps.is_row_local or caps.is_global or caps.needs_global_row_identity:
        return f"requires_global_execution:{name}:{strategy}"
    return None


def _find_table(config: dict[str, Any], table: str) -> dict[str, Any] | None:
    for tbl in config.get("tables", ()):
        if tbl.get("name") == table:
            return tbl
    return None


__all__ = [
    "NativeEligibility",
    "NativeExecutionPlan",
    "NativePlanNode",
    "compile_native_plan",
    "native_route_eligibility",
]

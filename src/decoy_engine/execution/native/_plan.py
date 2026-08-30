"""NativeExecutionPlan compiler + the public native-route eligibility query.

Task 0.2b. The architecture (Part A.1) names ``NativeExecutionPlan`` as the
backend-neutral description Python compiles before opening any DuckDB/Arrow
resource: per-node input projections, per-node execution requirements, the
per-output Arrow schema, the determinism draw family + key source per node, the
required prepasses / state tables, the diagnostic reducers, and the per-node
fallback policy. This module produces that object and attaches the resolved
requirements to each ``WorkNode`` inertly (nothing routes on them in Phase 0).

``native_route_eligibility`` is the public compatibility query the platform's
streaming-eligibility path is intended to consult instead of hard-coding a
strategy set. It is not yet wired into production routing (the platform's
``classify_streaming_eligibility`` currently routes on chunked-compatibility;
adopting this query is Task 2.7's integration). It reads
``StrategyCapabilities`` per column rather than recompiling: a
strategy is native-eligible when its output type is static AND it is row-local
AND it needs no whole-column pass or durable global row number. Every miss is a
coded rejection, so the platform gets machine-readable reasons and the set can
never silently drift from the live strategy registry.

The capability check alone is strategy-only: it says nothing about whether
THIS column's resolved config or input type is one the native kernel can
actually run. A `hash` column over a float or a tz-naive timestamp, an
invalid `truncate` length/keep/mask_char, or a non-string `redact_with`
would all pass the capability check and then fail mid-execution instead of
rerouting to the oracle at preflight. `profile` is optional for the
capability-only classification (composite/FK-aware, config-only); passing
it additionally unlocks the input-type check for `hash` (it needs the
source schema to resolve a column's Arrow type). Omitting it defers that
one check rather than guessing -- documented on `hash_config_rejection`
in `_requirements.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pyarrow as pa

from decoy_engine.execution._runner import (
    WorkNode,
    build_work_list,
    provider_is_composite,
)
from decoy_engine.execution.native._capabilities import (
    StrategyCapabilities,
    capabilities_for,
)
from decoy_engine.execution.native._requirements import (
    NodeRequirements,
    hash_config_rejection,
    native_kernel_rejection,
    redact_config_rejection,
    requirements_for,
    truncate_config_rejection,
)
from decoy_engine.plan import compile_plan
from decoy_engine.plan._seed_envelope import composite_fk_relationships
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


def _column_strategy_key(strategy: str, provider: str | None, registry: Any) -> str:
    """The capabilities key a config column resolves to.

    Mirrors ``build_work_list`` via the shared ``provider_is_composite``
    predicate: a composite provider fans out to a ``<composite>`` node regardless
    of the column's declared strategy string; every other column keys on its
    scalar strategy.
    """
    if provider_is_composite(provider, registry):
        return "<composite>"
    return strategy


def native_route_eligibility(
    config: dict[str, Any], *, table: str, profile: Any | None = None
) -> NativeEligibility:
    """Report whether ``table`` in ``config`` can run on the native route.

    Agrees with ``compile_native_plan`` on EVERY node kind by construction,
    because it classifies each node through the same shared predicates the
    WorkNode path uses:

    - scalar / provider-composite columns: ``_column_strategy_key`` (config-only,
      via ``provider_is_composite``), so a composite-provider column is excluded
      exactly as the WorkNode path excludes it.
    - FK-composite-group nodes: driven by ``profile.relationships`` (a composite
      FK collapses its child columns into one ``<group>`` node), NOT by any
      column string. That predicate is invisible without the profile, so
      evaluating it requires ``profile``. Pass it for full agreement; omit it and
      FK-group nodes are not evaluated (safe only when the table has no composite
      FK, which the caller must know).

    A node is native-eligible when its resolved capabilities are static-type,
    row-local, and need no whole-column pass or durable global row number.
    Generation columns are not on the native mask route yet. Every miss is a
    coded rejection.
    """
    table_cfg = _find_table(config, table)
    if table_cfg is None and profile is None:
        # No table config and no profile: nothing to classify.
        return NativeEligibility(accepted=True, rejections=())

    registry = get_default_registry()
    rejections: list[str] = []

    if table_cfg is not None:
        if table_cfg.get("generate_columns"):
            rejections.append("generation_not_native_route:generate_columns")
        for col in table_cfg.get("columns", ()):
            reason = _column_rejection(col, registry, table=table, profile=profile)
            if reason is not None:
                rejections.append(reason)

    if profile is not None:
        rejections.extend(_fk_group_rejections(profile, table))

    return NativeEligibility(accepted=not rejections, rejections=tuple(rejections))


def _column_rejection(
    col: dict[str, Any], registry: Any, *, table: str, profile: Any | None
) -> str | None:
    """The coded reason a single config column cannot run natively, or None.

    Alongside the capability gate, a strategy must have an actual compiled
    native kernel (`native_kernel_rejection`): row-local, static-typed
    capabilities describe many strategies (fpe, date_shift, bucketize among
    them) that have no kernel built yet, and admitting those here would let
    Task 2.7's dispatch reach a strategy it has nothing to run. The
    capability check runs first so a column failing both gates gets the
    more specific capability reason (e.g. `output_type_indeterminate`) over
    the blanket `no_native_kernel`; a column that passes capabilities but
    still lacks a kernel gets `no_native_kernel`. After both gates pass, the
    admitted set gets one more, strategy-specific check: a `hash` column's
    resolved input type, a `truncate` column's length/keep/mask_char shape,
    a `redact` column's `redact_with` type. These all run through the same
    functions `requirements_for` (`_requirements.py`) calls to compute a
    compiled node's `fallback_policy`, so this config-only query and the
    compiler reach the same verdict on every column without forking the
    rules.
    """
    name = col.get("name", "?")
    strategy = col.get("strategy")
    if not strategy:
        return f"missing_strategy:{name}"
    provider = col.get("provider")
    resolved = _column_strategy_key(strategy, provider, registry)
    if resolved == "<composite>":
        # Matches the WorkNode path: a composite provider fans out to a
        # multi-column node with an indeterminate per-column output type.
        return f"composite_provider_multi_column:{name}:{provider}"
    try:
        caps = capabilities_for(resolved)
    except KeyError:
        return f"unclassified_strategy:{name}:{resolved}"
    reason = _native_rejection(name, resolved, caps)
    if reason is not None:
        return reason
    kernel_reason = native_kernel_rejection(name, resolved)
    if kernel_reason is not None:
        return kernel_reason
    return _config_rejection(name, resolved, col, table=table, profile=profile)


def _config_rejection(
    name: str, strategy: str, col: dict[str, Any], *, table: str, profile: Any | None
) -> str | None:
    """The coded reason `name`'s resolved CONFIG or INPUT type is one the
    native kernel for `strategy` cannot honor, or None. Only the admitted
    set's four strategies get a gate here; every other strategy that passed
    the capability check above is unaffected (narrowing, never widening)."""
    if strategy == "hash":
        return hash_config_rejection(name, table, profile)
    if strategy == "truncate":
        provider_config = col.get("provider_config")
        return truncate_config_rejection(
            name, provider_config if isinstance(provider_config, dict) else {}
        )
    if strategy == "redact":
        provider_config = col.get("provider_config")
        return redact_config_rejection(
            name, provider_config if isinstance(provider_config, dict) else {}
        )
    return None


def _fk_group_rejections(profile: Any, table: str) -> list[str]:
    """Coded rejections for the FK-composite-group nodes on ``table``.

    Evaluated through ``capabilities_for("<group>")`` and the same
    ``_native_rejection`` logic ``compile_native_plan`` applies to the
    ``composite_fk_group`` node, so the two verdicts match by construction. If
    ``<group>``'s capabilities ever change to non-native, both APIs reject
    together.
    """
    rejections: list[str] = []
    caps = capabilities_for("<group>")
    for rel in composite_fk_relationships(profile):
        if rel.child_table != table:
            continue
        canonical_key = "__".join(sorted(rel.child_columns))
        reason = _native_rejection(canonical_key, "<group>", caps)
        if reason is not None:
            rejections.append(reason)
    return rejections


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

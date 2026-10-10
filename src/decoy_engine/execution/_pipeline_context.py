"""The resolve and route phases of `run_pipeline`, as a typed carrier.

`run_pipeline` used to hold every resolved value as a local and hand the whole bag to the
unified-slice lane through `locals()`. The resolve phase now ends in one frozen
`PipelineRunContext`, the route phase in one frozen `RouteDecision`, and every layer-1 route
executor takes `(ctx, decision)`.

Phases (each frozen at its own phase boundary):

1. `build_run_context` validates the knobs, selects the adapter, profiles, compiles the plan,
   resolves the keyed-mask secret, builds the relationship graph and prepares engine-owned
   table transforms. It runs in exactly the order the inline code did, so a bad input raises
   the same error before the same expensive step.
2. `decide_route` calls the frozen routing helpers (`_pipeline_routing`) and the out-of-core
   decline check, in the old order. It decides; it executes nothing.
3. Executors build their own route-local state. `resident_sources` and the `PoolCache` are
   full_frame-only and are created inside the full_frame executor, never here, so a bounded
   route can still return or reject before any source is materialized.

Freezing is shallow: field bindings cannot change, but the source mappings, sink, registry
and caches keep whatever mutability the code relied on. Construction only reads its inputs.
The context carries RESOLVED values under the plain names (`registry` and `key_provider` are
the resolved ones; the raw arguments are not kept), so no executor can reach for the unresolved
form by mistake.

`classify_table_kinds` lives here because the builder needs it and `_pipeline` imports this
module; `_pipeline` re-exports it, so its public import path is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution import _pipeline_auto_chunk, _pipeline_routing
from decoy_engine.execution._chunked_output_sink import OutputPublish
from decoy_engine.execution._transforms_admission import out_of_core_declined, routing_profile
from decoy_engine.execution._transforms_gate import reject_any_per_table_transforms
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from faker import Faker

    from decoy_engine.execution._adapter import ExecutionAdapter
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.execution._transforms_prepare import PreparedSources
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._types import Profile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "PipelineRunContext",
    "RouteDecision",
    "build_run_context",
    "classify_table_kinds",
    "decide_route",
]


def classify_table_kinds(config: dict[str, Any]) -> dict[str, str]:
    """Return `{table_name: "mask" | "generate"}` for every table in the config.

    Per-table kind is inferred from `columns` (mask) vs `generate_columns`
    (generate) presence on each TableConfig. The schema already enforces
    XOR at validation time (`_per_table_kind_consistency` + `TableConfig`
    invariants), so a config that reaches this helper has at most one
    populated per table. Tables with neither are classified as mask
    (defensive default; the schema rejects them upstream).
    """
    out: dict[str, str] = {}
    for table in config.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = table.get("name")
        if not isinstance(name, str):
            continue
        if table.get("generate_columns"):
            out[name] = "generate"
        else:
            out[name] = "mask"
    return out


@dataclass(frozen=True)
class PipelineRunContext:
    """Everything resolved before routing. `registry` and `key_provider` are the RESOLVED ones."""

    config: dict[str, Any]
    engine_version: str
    # Resolved run state.
    plan: Plan
    profile: Profile
    graph: RelationshipGraph
    namespace_registry: Any
    registry: ProviderRegistry
    key_provider: KeyProvider | None
    adapter: ExecutionAdapter
    publish: OutputPublish
    # Post-transform-preparation sources: what routing priced and every route reads.
    caller_sources: dict[str, pa.Table | LazySource]
    prepared: PreparedSources
    table_kinds: dict[str, str]
    has_mask_table: bool
    has_generate_table: bool
    projection_policy: UnconfiguredColumnPolicy
    generate_output_tables: frozenset[str]
    # Caller handles.
    sink: TransactionalSink | None
    source_loader: Callable[[str], pa.Table] | None
    vault_writer: Any
    derive_key: Any
    instance_default_locale: str | None
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None
    # Knobs.
    execution_mode: str
    substrate: str | None
    resolved_substrate: str
    fpe_chunk_count: int
    max_workers: int
    fallback_to_pandas: bool
    explain_plan: bool
    auto_chunk: bool
    chunk_size_rows: int
    auto_chunk_threshold_rows: int
    native_threads: int
    chunked_dispatcher_enabled: bool
    multi_table_dispatch_enabled: bool
    out_of_core_threshold_rows: int
    full_frame_reject_rows: int
    out_of_core_budget_bytes: int | None
    use_byte_estimate_routing: bool
    use_probe_routing: bool
    out_of_core_reorder_threshold_rows: int | None
    unified_slice_enabled: bool
    fidelity_report: bool
    post_validation: bool
    post_validation_skip: list[str]
    post_validation_sample_size: int
    post_validation_enforce: bool
    now_iso: str | None


@dataclass(frozen=True)
class RouteDecision:
    """The two routing layers' verdicts plus the out-of-core decline, computed once."""

    route: str
    route_reason: str
    execution_plan_decision: ExecutionPlan | None
    route_chunked: bool
    keep_lazy: Mapping[str, Any]
    ooc_declined: str | None


def build_run_context(
    config: dict[str, Any],
    sources: Mapping[str, pa.Table | LazySource] | None,
    *,
    engine_version: str,
    registry: ProviderRegistry | None,
    derive_key: Any,
    instance_default_locale: str | None,
    vault_writer: Any,
    fidelity_report: bool,
    post_validation: bool,
    post_validation_skip: list[str] | None,
    post_validation_sample_size: int,
    post_validation_enforce: bool,
    now_iso: str | None,
    execution_mode: str,
    sink: TransactionalSink | None,
    source_loader: Callable[[str], pa.Table] | None,
    substrate: str | None,
    fpe_chunk_count: int,
    max_workers: int,
    fallback_to_pandas: bool,
    explain_plan: bool,
    auto_chunk: bool,
    chunk_size_rows: int,
    auto_chunk_threshold_rows: int,
    native_threads: int,
    chunked_dispatcher_enabled: bool,
    stream_chunked_output: bool,
    multi_table_dispatch_enabled: bool,
    out_of_core_threshold_rows: int,
    full_frame_reject_rows: int,
    out_of_core_budget_bytes: int | None,
    use_byte_estimate_routing: bool,
    use_probe_routing: bool,
    key_provider: KeyProvider | None,
    out_of_core_reorder_threshold_rows: int | None,
    unified_slice_enabled: bool,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
    prepare_sources: Callable[..., PreparedSources],
) -> PipelineRunContext:
    """Resolve and validate every run input, in the order the inline code did.

    `prepare_sources` is `_transforms_prepare.prepare_transform_sources`, handed in by
    `run_pipeline` so the call resolves through `_pipeline`'s module namespace at run time.
    Existing tests count its calls by patching `_pipeline.prepare_transform_sources`.
    """
    from decoy_engine.execution._output_projection import resolve_unconfigured_column_policy
    from decoy_engine.execution._substrate import (
        require_bool,
        require_positive_int,
        resolve_substrate,
        select_execution_adapter,
    )
    from decoy_engine.execution.out_of_core._route_policy import resolve_reorder_threshold_rows
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        RelationshipGraph,
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    # Adapter selection runs up front, before any profiling or plan
    # compilation, so an invalid substrate or count knob fails at submit
    # time with a typed error instead of after the expensive stages.
    # Construction is cheap and side-effect free for both adapters, so
    # pure-generate jobs (which never use it) lose nothing.
    resolved_substrate = resolve_substrate(substrate)
    adapter = select_execution_adapter(
        substrate=resolved_substrate,
        fpe_chunk_count=fpe_chunk_count,
        max_workers=max_workers,
        fallback_to_pandas=fallback_to_pandas,
    )
    # Auto-chunk knobs share the substrate knobs' fail-early contract.
    require_bool("auto_chunk", auto_chunk)
    require_positive_int("chunk_size_rows", chunk_size_rows)
    require_positive_int("auto_chunk_threshold_rows", auto_chunk_threshold_rows)
    _pipeline_auto_chunk.require_lane_knobs(native_threads, chunked_dispatcher_enabled)
    publish = OutputPublish(sink, stream_chunked_output, post_validation)  # B6a; validates the knob
    require_bool("multi_table_dispatch_enabled", multi_table_dispatch_enabled)
    # SC2 out-of-core routing thresholds share the same fail-early contract.
    require_positive_int("out_of_core_threshold_rows", out_of_core_threshold_rows)
    require_positive_int("full_frame_reject_rows", full_frame_reject_rows)
    if out_of_core_budget_bytes is not None:
        require_positive_int("out_of_core_budget_bytes", out_of_core_budget_bytes)
    require_bool("use_byte_estimate_routing", use_byte_estimate_routing)
    require_bool("use_probe_routing", use_probe_routing)
    require_bool("unified_slice_enabled", unified_slice_enabled)
    # A1 post-validation knobs share the substrate knobs' fail-early contract.
    require_bool("post_validation", post_validation)
    require_bool("post_validation_enforce", post_validation_enforce)
    require_positive_int("post_validation_sample_size", post_validation_sample_size)
    resolve_reorder_threshold_rows(out_of_core_reorder_threshold_rows)
    if execution_mode == "out_of_core":
        # Config-only, so nothing is profiled or read before the refusal.
        reject_any_per_table_transforms(config, route="execution_mode='out_of_core'")

    # None-normalize the skip list here (a mutable [] default would be shared across calls).
    skip = list(post_validation_skip) if post_validation_skip else []

    resolved_registry = registry if registry is not None else get_default_registry()
    caller_sources: dict[str, pa.Table | LazySource] = dict(sources) if sources else {}

    table_kinds = classify_table_kinds(config)
    has_mask_table = any(kind == "mask" for kind in table_kinds.values())
    has_generate_table = any(kind == "generate" for kind in table_kinds.values())

    # DE-03: resolve the output-projection policy once; generate-kind tables ride
    # through the mask adapter as echoed sources and are exempt (declared by their
    # generate config, not the mask plan). Threaded into every emission route.
    projection_policy = resolve_unconfigured_column_policy(config)
    generate_output_tables = frozenset(
        name for name, kind in table_kinds.items() if kind == "generate"
    )

    # F5 (2026-06-26): route the profile-path seed through the canonical
    # int normalizer so a bool/float seed is rejected here, BEFORE
    # profile_source seeds its RNG, rather than being silently coerced
    # (`seed: true` -> random.Random(True) == Random(1)) and only caught
    # later by compile_plan. Defaults absent seed to 0, matching the
    # compiler so the profile and mask paths stay in lockstep.
    from decoy_engine.plan._seed import _normalize_job_seed_int

    job_seed = _normalize_job_seed_int(config)

    profile = profile_source(config, seed=job_seed)

    plan = compile_plan(config, profile, decoy_engine_version=engine_version)

    # DE-02 fail-closed gate: resolve the keyed-mask secret ONCE, before any table
    # / quarantine / vault / manifest is written. Pre-GA a keyed plan with no
    # secret falls back to job_seed (byte-identical); at GA it hard-errors
    # (KeyedStrategyRequiresSecret). The secret is a reference in config
    # (`global_settings.mask_secret_ref`, env:/file:), never serialized raw, and a
    # programmatic `key_provider` wins over the ref. The resolved provider threads
    # into every execution route; None means "no secret -> job_seed".
    from decoy_engine.keyprovider import mask_key_from_provider, resolve_key_provider

    resolved_key_provider = resolve_key_provider(
        plan=plan,
        key_provider=key_provider,
        mask_secret_ref=(config.get("global_settings") or {}).get("mask_secret_ref"),
    )
    # DE-02 (Codex BLOCKER 5 / item 6a): the token vault holds reversible plaintext
    # PII and must be encrypted under the SAME resolved mask key as the masking
    # run. Fail closed (shared guard) if a caller-supplied vault writer is keyed
    # differently, or is not the standard VaultWriter contract.
    if vault_writer is not None:
        from decoy_engine.vault import assert_vault_writer_keyed

        assert_vault_writer_keyed(
            vault_writer,
            mask_key_from_provider(resolved_key_provider, plan.seed_envelope.job_seed),
        )

    ns_registry = build_namespace_registry(config, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships,
            namespace_registry=ns_registry,
            orphan_policy_lookup=lookup,
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())

    # Resident transform-bearing tables are transformed once, here, so routing prices
    # the data that will run; the raw tables are no longer referenced from `caller_sources`.
    prepared = prepare_sources(
        config,
        caller_sources,
        profile=profile,
        graph=graph,
        execution_mode=execution_mode,
        has_generate_table=has_generate_table,
        has_mask_table=has_mask_table,
        validators=(config.get("validators") or []),
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
        post_validation=post_validation,
        resolved_substrate=resolved_substrate,
    )
    return PipelineRunContext(
        config=config,
        engine_version=engine_version,
        plan=plan,
        profile=profile,
        graph=graph,
        namespace_registry=ns_registry,
        registry=resolved_registry,
        key_provider=resolved_key_provider,
        adapter=adapter,
        publish=publish,
        caller_sources=prepared.sources,
        prepared=prepared,
        table_kinds=table_kinds,
        has_mask_table=has_mask_table,
        has_generate_table=has_generate_table,
        projection_policy=projection_policy,
        generate_output_tables=generate_output_tables,
        sink=sink,
        source_loader=source_loader,
        vault_writer=vault_writer,
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
        provider_snapshot=provider_snapshot,
        execution_mode=execution_mode,
        substrate=substrate,
        resolved_substrate=resolved_substrate,
        fpe_chunk_count=fpe_chunk_count,
        max_workers=max_workers,
        fallback_to_pandas=fallback_to_pandas,
        explain_plan=explain_plan,
        auto_chunk=auto_chunk,
        chunk_size_rows=chunk_size_rows,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
        native_threads=native_threads,
        chunked_dispatcher_enabled=chunked_dispatcher_enabled,
        multi_table_dispatch_enabled=multi_table_dispatch_enabled,
        out_of_core_threshold_rows=out_of_core_threshold_rows,
        full_frame_reject_rows=full_frame_reject_rows,
        out_of_core_budget_bytes=out_of_core_budget_bytes,
        use_byte_estimate_routing=use_byte_estimate_routing,
        use_probe_routing=use_probe_routing,
        out_of_core_reorder_threshold_rows=out_of_core_reorder_threshold_rows,
        unified_slice_enabled=unified_slice_enabled,
        fidelity_report=fidelity_report,
        post_validation=post_validation,
        post_validation_skip=skip,
        post_validation_sample_size=post_validation_sample_size,
        post_validation_enforce=post_validation_enforce,
        now_iso=now_iso,
    )


def decide_route(ctx: PipelineRunContext) -> RouteDecision:
    """Routing layer 1 (S2 + SC2), layer 2 (S3 auto-chunk) and the out-of-core decline check.

    Layer 1: relationship-bearing pure-mask jobs take a bounded-memory route (out-of-core when
    large + compatible, else sequential); a large FK job no bounded route can take is rejected
    before read. That is an early return / a fail-closed raise. The SC2 admission + size signals
    are inert (False/None/None/True) off the relationship+mask shape, so non-FK jobs keep the
    pre-SC2 routing. The size signal comes from the (SC7a bounded) profile metadata, so the
    gates fire on the lazy `source_loader` path too (SC7b, closing the F2 reject-before-read hole).

    Layer 2 classification is computed before any layer-1 early return so `explain_plan`
    surfaces it on every route, relationship jobs included. `keep_lazy` is the B6b footer
    snapshot of lazy candidates; see `_pipeline_routing`.
    """
    route, route_reason = _pipeline_routing.resolve_execution_route(
        routing_profile(ctx.profile, ctx.caller_sources, ctx.prepared.prepared),
        plan=ctx.plan,
        registry=ctx.registry,
        graph=ctx.graph,
        caller_sources=ctx.caller_sources,
        table_kinds=ctx.table_kinds,
        has_mask_table=ctx.has_mask_table,
        has_generate_table=ctx.has_generate_table,
        validators=(ctx.config.get("validators") or []),
        fidelity_report=ctx.fidelity_report,
        post_validation=ctx.post_validation,
        vault_writer=ctx.vault_writer,
        execution_mode=ctx.execution_mode,
        resolved_substrate=ctx.resolved_substrate,
        out_of_core_threshold_rows=ctx.out_of_core_threshold_rows,
        full_frame_reject_rows=ctx.full_frame_reject_rows,
        out_of_core_budget_bytes=ctx.out_of_core_budget_bytes,
        use_byte_estimate_routing=ctx.use_byte_estimate_routing,
        use_probe_routing=ctx.use_probe_routing,
        config=ctx.config,
        engine_version=ctx.engine_version,
        prepared_tables=ctx.prepared.prepared,
    )
    execution_plan_decision, route_chunked, keep_lazy = _pipeline_routing.decide_chunk_route(
        ctx.config,
        plan=ctx.plan,
        registry=ctx.registry,
        graph=ctx.graph,
        substrate=ctx.resolved_substrate,
        caller_sources=ctx.caller_sources,
        auto_chunk_threshold_rows=ctx.auto_chunk_threshold_rows,
        explain_plan=ctx.explain_plan,
        auto_chunk=ctx.auto_chunk,
        has_mask_table=ctx.has_mask_table,
        table_kinds=ctx.table_kinds,
        prepared_tables=ctx.prepared.prepared,
    )
    ooc_declined = out_of_core_declined(
        ctx.config,
        plan=ctx.plan,
        registry=ctx.registry,
        graph=ctx.graph,
        profile=ctx.profile,
        table_kinds=ctx.table_kinds,
        execution_mode=ctx.execution_mode,
    )
    return RouteDecision(
        route=route,
        route_reason=route_reason,
        execution_plan_decision=execution_plan_decision,
        route_chunked=route_chunked,
        keep_lazy=keep_lazy,
        ooc_declined=ooc_declined,
    )


def unified_slice_locals(
    ctx: PipelineRunContext,
    decision: RouteDecision,
    caller_sources: Mapping[str, pa.Table | LazySource],
    pool_cache: Any,
) -> dict[str, Any]:
    """The name-to-value mapping `_unified_slice.run_from_pipeline_locals` used to receive as
    `run_pipeline`'s own `locals()`, rebuilt from the context. Transitional: it exists so the
    forwarding mechanism can move out of `run_pipeline` before the context is threaded into
    `maybe_run_unified_slice` directly, and goes away with that forwarding.

    `caller_sources` is the full_frame executor's own post-resolution mapping, and
    `pool_cache` its single per-job cache; neither exists on the context.
    """
    return {
        "unified_slice_enabled": ctx.unified_slice_enabled,
        "adapter": ctx.adapter,
        "config": ctx.config,
        "plan": ctx.plan,
        "profile": ctx.profile,
        "graph": ctx.graph,
        "table_kinds": ctx.table_kinds,
        "caller_sources": caller_sources,
        "source_loader": ctx.source_loader,
        "sink": ctx.sink,
        "fidelity_report": ctx.fidelity_report,
        "post_validation": ctx.post_validation,
        "vault_writer": ctx.vault_writer,
        "route": decision.route,
        "route_chunked": decision.route_chunked,
        "substrate": ctx.substrate,
        "resolved_substrate": ctx.resolved_substrate,
        "fpe_chunk_count": ctx.fpe_chunk_count,
        "max_workers": ctx.max_workers,
        "fallback_to_pandas": ctx.fallback_to_pandas,
        "auto_chunk": ctx.auto_chunk,
        "chunk_size_rows": ctx.chunk_size_rows,
        "auto_chunk_threshold_rows": ctx.auto_chunk_threshold_rows,
        "out_of_core_threshold_rows": ctx.out_of_core_threshold_rows,
        "full_frame_reject_rows": ctx.full_frame_reject_rows,
        "use_byte_estimate_routing": ctx.use_byte_estimate_routing,
        "use_probe_routing": ctx.use_probe_routing,
        "out_of_core_budget_bytes": ctx.out_of_core_budget_bytes,
        "out_of_core_reorder_threshold_rows": ctx.out_of_core_reorder_threshold_rows,
        "execution_mode": ctx.execution_mode,
        "explain_plan": ctx.explain_plan,
        "execution_plan_decision": decision.execution_plan_decision,
        "route_reason": decision.route_reason,
        "engine_version": ctx.engine_version,
        "pool_cache": pool_cache,
        "native_threads": ctx.native_threads,
        "resolved_registry": ctx.registry,
        "resolved_key_provider": ctx.key_provider,
    }

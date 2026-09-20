"""`capture_physical_plan_inputs`: builds a real `PhysicalPlanInputs` (D1) by
running the SAME preflight sequence `run_pipeline` runs before it ever
dispatches to a driver (`profile_source` -> `compile_plan` ->
`build_namespace_registry`/`build_relationship_graph` -> the routing-signal
resolvers).

This function is the concrete "how" behind D1's snapshot -- and the D4
harness's way of building a real, non-synthesized input snapshot from a real
job -- but it is NOT `run_pipeline` and is NOT called by it: it stops the
moment every routing-relevant fact is captured, before any driver would be
invoked. Nothing here masks a row.
"""

from __future__ import annotations

import types
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution import _pipeline_finalize
from decoy_engine.execution._planner import (
    AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT,
    FULL_FRAME_REJECT_ROWS_DEFAULT,
    OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
)
from decoy_engine.execution.physical._inputs import (
    OutOfCoreRoutingFacts,
    PhysicalPlanInputs,
    deep_freeze_config,
)
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.providers_v2 import ProviderRegistry

__all__ = ["capture_physical_plan_inputs"]


def _sink_class_token(sink: TransactionalSink | None) -> str:
    if sink is None:
        return "None"
    cls = type(sink)
    return f"{cls.__module__}.{cls.__qualname__}"


def capture_physical_plan_inputs(
    config: dict[str, Any],
    sources: Mapping[str, pa.Table | LazySource] | None = None,
    *,
    engine_version: str,
    registry: ProviderRegistry | None = None,
    fidelity_report: bool = False,
    execution_mode: str = "auto",
    sink: TransactionalSink | None = None,
    source_loader: Callable[[str], pa.Table] | None = None,
    substrate: str | None = _pipeline_finalize.SUBSTRATE_DEFAULT,
    fpe_chunk_count: int = _pipeline_finalize.FPE_CHUNK_COUNT_DEFAULT,
    max_workers: int = _pipeline_finalize.MAX_WORKERS_DEFAULT,
    fallback_to_pandas: bool = _pipeline_finalize.FALLBACK_TO_PANDAS_DEFAULT,
    auto_chunk: bool = _pipeline_finalize.AUTO_CHUNK_DEFAULT,
    chunk_size_rows: int = _pipeline_finalize.CHUNK_SIZE_ROWS_DEFAULT,
    auto_chunk_threshold_rows: int = AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT,
    out_of_core_threshold_rows: int = OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
    full_frame_reject_rows: int = FULL_FRAME_REJECT_ROWS_DEFAULT,
    out_of_core_budget_bytes: int | None = None,
    use_byte_estimate_routing: bool = True,
    use_probe_routing: bool = True,
    out_of_core_reorder_threshold_rows: int | None = None,
    vault_writer: Any = None,
) -> PhysicalPlanInputs:
    """Build a `PhysicalPlanInputs` snapshot for `config`/`sources` by running
    the real preflight sequence. Argument defaults mirror `run_pipeline`'s own
    (`_pipeline_finalize` / `_planner` constants) so an unspecified knob here
    captures the same routing facts an unspecified `run_pipeline` kwarg would.
    """
    from decoy_engine.execution._pipeline import classify_table_kinds
    from decoy_engine.execution._pipeline_routing_signals import (
        out_of_core_routing_signals,
        resolve_full_frame_fits_estimate,
        resolve_probe_recovery,
    )
    from decoy_engine.execution._substrate import (
        require_bool,
        require_positive_int,
        resolve_substrate,
        select_execution_adapter,
    )
    from decoy_engine.execution.native._companion_status import native_companion_status
    from decoy_engine.execution.out_of_core import resolve_budget
    from decoy_engine.execution.out_of_core._route_policy import (
        _MERGE_FAN_IN_DEFAULT,
        resolve_reorder_threshold_rows,
    )
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        RelationshipGraph,
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    # Submit-boundary validation (Task 4.3 remediation MED): the SAME live
    # checks `run_pipeline` runs at `_pipeline.py:264-283`, in the SAME
    # order, BEFORE any preflight work below -- so an invalid knob (a string
    # `auto_chunk`, a non-positive count) raises the identical coded
    # `ExecutionError` here that `run_pipeline` would raise, instead of
    # silently producing a compilable snapshot. `select_execution_adapter`'s
    # construction is cheap and side-effect-free (it does not execute), so
    # calling it purely for its validation side effect matches production's
    # own "adapter selection runs up front" comment.
    resolved_substrate = resolve_substrate(substrate)
    select_execution_adapter(
        substrate=resolved_substrate,
        fpe_chunk_count=fpe_chunk_count,
        max_workers=max_workers,
        fallback_to_pandas=fallback_to_pandas,
    )
    require_bool("auto_chunk", auto_chunk)
    require_positive_int("chunk_size_rows", chunk_size_rows)
    require_positive_int("auto_chunk_threshold_rows", auto_chunk_threshold_rows)
    require_positive_int("out_of_core_threshold_rows", out_of_core_threshold_rows)
    require_positive_int("full_frame_reject_rows", full_frame_reject_rows)
    if out_of_core_budget_bytes is not None:
        require_positive_int("out_of_core_budget_bytes", out_of_core_budget_bytes)
    require_bool("use_byte_estimate_routing", use_byte_estimate_routing)
    require_bool("use_probe_routing", use_probe_routing)
    resolved_reorder_threshold = resolve_reorder_threshold_rows(out_of_core_reorder_threshold_rows)

    resolved_registry = registry if registry is not None else get_default_registry()
    caller_sources: dict[str, pa.Table | LazySource] = dict(sources) if sources else {}
    # Validators are a config-only, route-affecting input (a truthy list disqualifies
    # the bounded routes via _sequential_eligible -> "validators_present"). run_pipeline
    # reads them only from config (`_pipeline.py`: validators=config.get("validators") or []),
    # so the snapshot must too -- never from a caller kwarg, which could diverge.
    config_validators = list(config.get("validators") or [])

    # Frozen against post-capture mutation (Task 4.3 remediation H1): a plain
    # dict returned by `classify_table_kinds` is otherwise mutable in place,
    # which would silently invalidate `plan_hash`'s snapshot-content
    # contract. `pa.Table` / `Plan` stay un-copied (heavy, unnecessary --
    # `plan_hash` now covers their route-affecting content directly). Kept
    # as a plain `dict` through this function's own preflight calls below
    # (several live signature `dict[str, str]` invariantly, not `Mapping`)
    # and frozen into a `MappingProxyType` only at the final `PhysicalPlan
    # Inputs` construction.
    table_kinds = classify_table_kinds(config)
    has_mask_table = any(kind == "mask" for kind in table_kinds.values())

    job_seed = _normalize_job_seed_int(config)
    profile = profile_source(config, seed=job_seed)
    plan = compile_plan(config, profile, decoy_engine_version=engine_version)

    ns_registry = build_namespace_registry(config, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships, namespace_registry=ns_registry, orphan_policy_lookup=lookup
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())

    (
        out_of_core_compatible,
        out_of_core_reject_code,
        largest_table_rows,
        largest_table_rows_exact,
    ) = out_of_core_routing_signals(
        profile,
        plan=plan,
        registry=resolved_registry,
        graph=graph,
        caller_sources=caller_sources,
        table_kinds=table_kinds,
        has_mask_table=has_mask_table,
    )
    full_frame_fits_estimate = resolve_full_frame_fits_estimate(
        use_byte_estimate_routing, profile, caller_sources, table_kinds, out_of_core_budget_bytes
    )
    probe_recovers_full_frame = resolve_probe_recovery(
        use_probe_routing,
        use_byte_estimate_routing,
        profile,
        caller_sources,
        table_kinds,
        out_of_core_budget_bytes,
        full_frame_fits_estimate,
        config=config,
        engine_version=engine_version,
    )
    resolved_budget = resolve_budget(out_of_core_budget_bytes)

    # Native companion probe outcome (Task 4.3 remediation H3; design doc
    # section 12 punch-list): read-only, never-raises, no-masking (see
    # `PhysicalPlanInputs.native_companion_reason`'s docstring for scope).
    native_companion_reason = native_companion_status().reason

    out_of_core_facts = OutOfCoreRoutingFacts(
        compatible=out_of_core_compatible,
        reject_code=out_of_core_reject_code,
        largest_table_rows=largest_table_rows,
        largest_table_rows_exact=largest_table_rows_exact,
        full_frame_fits_estimate=full_frame_fits_estimate,
        probe_recovers_full_frame=probe_recovers_full_frame,
        budget_bytes=resolved_budget.budget_bytes,
        reorder_threshold_rows=resolved_reorder_threshold,
        merge_fan_in=_MERGE_FAN_IN_DEFAULT,
    )

    return PhysicalPlanInputs(
        # Deep-frozen so the stored snapshot is genuinely immutable: the
        # compiler re-reads config content via `classify_job`, so a config
        # mutated after capture would change the driver while `plan_hash`
        # stayed put (Codex final-gate HIGH). `thaw_config` reverses it at
        # the one compile-time consumer.
        config=deep_freeze_config(config),
        plan=plan,
        profile=profile,
        registry=resolved_registry,
        graph=graph,
        table_kinds=types.MappingProxyType(table_kinds),
        caller_sources=caller_sources,
        source_loader_present=source_loader is not None,
        resolved_substrate=resolved_substrate,
        sink_class_token=_sink_class_token(sink),
        execution_mode=execution_mode,
        fidelity_report=fidelity_report,
        vault_writer_present=vault_writer is not None,
        validators=tuple(config_validators),
        auto_chunk=auto_chunk,
        chunk_size_rows=chunk_size_rows,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
        out_of_core_threshold_rows=out_of_core_threshold_rows,
        full_frame_reject_rows=full_frame_reject_rows,
        use_byte_estimate_routing=use_byte_estimate_routing,
        use_probe_routing=use_probe_routing,
        fpe_chunk_count=fpe_chunk_count,
        max_workers=max_workers,
        fallback_to_pandas=fallback_to_pandas,
        out_of_core_facts=out_of_core_facts,
        native_companion_reason=native_companion_reason,
        engine_version=engine_version,
    )

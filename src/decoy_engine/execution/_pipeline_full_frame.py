"""The full_frame layer-1 route executor, peer to the sequential and out-of-core executors.

This is the old inline tail of `run_pipeline`: build the route-local state, offer the job to the
unified-slice fast lane, and otherwise run the generate + mask steps, stitch, finalize,
stamp telemetry, run post-validation and publish.

Route-local state is created HERE and nowhere earlier. `resident_sources` materializes every
source and the `PoolCache` is the job's single Faker-pool cache, shared by the unified lane and
the full-frame oracle so a pool's provider code runs at most once even if the lane reroutes.
Creating either before a bounded route could return or reject would load data that route never
reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution import _pipeline_finalize, _pipeline_generate_mask
from decoy_engine.execution import _pipeline_route_exec as _route_exec
from decoy_engine.execution import _pipeline_sources as _psrc
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._pipeline_context import unified_slice_locals
from decoy_engine.execution._stitch import stitch_generate_mask_outputs
from decoy_engine.execution._transforms_admission import stamp_out_of_core_declined
from decoy_engine.execution._unified_slice import run_from_pipeline_locals
from decoy_engine.generation.pool import PoolCache

if TYPE_CHECKING:
    from decoy_engine.execution._pipeline_context import PipelineRunContext, RouteDecision

__all__ = ["run_full_frame_route"]


def run_full_frame_route(ctx: PipelineRunContext, decision: RouteDecision) -> ExecutionResult:
    # TB-1: only full_frame / auto-chunk below needs every source resident, and a loader-backed
    # job diverted here must still get its mask tables through the loader. A lazy route or
    # split candidate stays lazy (`keep_lazy`); the unified slice sees the rest as tables.
    resident_sources = _psrc.resolve_resident_sources(
        ctx.caller_sources,
        source_loader=ctx.source_loader,
        required_tables=[name for name, kind in ctx.table_kinds.items() if kind == "mask"],
        config=ctx.config,
        prepared=ctx.prepared.prepared,
        keep_lazy=decision.keep_lazy,
    )
    caller_sources = {k: v for k, v in resident_sources.items() if k in ctx.caller_sources}

    pool_cache = PoolCache()
    unified_slice_result = run_from_pipeline_locals(  # Task 4.5, see its docstring
        unified_slice_locals(ctx, decision, caller_sources, pool_cache)
    )
    if unified_slice_result is not None:
        return unified_slice_result

    publish = ctx.publish
    with publish:
        # Steps 1-2 (generate-kind tables, then mask-kind tables) live in
        # `_pipeline_generate_mask.run_generate_and_mask_steps` (LOC ceiling).
        step = _pipeline_generate_mask.run_generate_and_mask_steps(
            has_generate_table=ctx.has_generate_table,
            has_mask_table=ctx.has_mask_table,
            plan=ctx.plan,
            derive_key=ctx.derive_key,
            instance_default_locale=ctx.instance_default_locale,
            provider_snapshot=ctx.provider_snapshot,
            resident_sources=resident_sources,
            caller_sources=caller_sources,
            keep_lazy=decision.keep_lazy,
            route_chunked=decision.route_chunked,
            table_kinds=ctx.table_kinds,
            config=ctx.config,
            engine_version=ctx.engine_version,
            registry=ctx.registry,
            adapter=ctx.adapter,
            vault_writer=ctx.vault_writer,
            chunk_size_rows=ctx.chunk_size_rows,
            native_threads=ctx.native_threads,
            pool_cache=pool_cache,
            chunked_dispatcher_enabled=ctx.chunked_dispatcher_enabled,
            multi_table_dispatch_enabled=ctx.multi_table_dispatch_enabled,
            key_provider=ctx.key_provider,
            graph=ctx.graph,
            namespace_registry=ctx.namespace_registry,
            unconfigured_column_policy=ctx.projection_policy,
            generate_output_tables=ctx.generate_output_tables,
            substrate=ctx.substrate,
            resolved_substrate=ctx.resolved_substrate,
            fpe_chunk_count=ctx.fpe_chunk_count,
            max_workers=ctx.max_workers,
            fallback_to_pandas=ctx.fallback_to_pandas,
            auto_chunk=ctx.auto_chunk,
            auto_chunk_threshold_rows=ctx.auto_chunk_threshold_rows,
            execution_plan_decision=decision.execution_plan_decision,
            fidelity_report=ctx.fidelity_report,
            now_iso=ctx.now_iso,
            publish=publish,
        )
        # Step 3: stitch the outputs together via the shared helper both this
        # oracle and the shadow coordinator's mixed dispatch call, so "mask wins
        # ties" cannot drift between the two (Task 4.6 slice 5b-i).
        outputs: dict[str, pa.Table] = stitch_generate_mask_outputs(
            step.generate_outputs, step.mask_outputs
        )

        # BF1: namespace the fidelity reports under the existing free-form
        # quality_metrics dict (already plumbed to the platform manifest).
        # Additive + default-OFF: when the flag is off, fidelity_reports is
        # empty and quality_metrics is untouched.
        quality_metrics: dict[str, Any] = dict(step.mask_quality_metrics)
        if step.fidelity_reports:
            quality_metrics["fidelity_reports"] = step.fidelity_reports

        # Explain surfacing: stamp the static job-level classification (computed once
        # above); a multi-table split is in auto_chunk.tables, not here. Default-off
        # flag; default runs stamp nothing here.
        execution_plan_decision = decision.execution_plan_decision
        if ctx.explain_plan and execution_plan_decision is not None:
            quality_metrics["execution_plan"] = {
                "mode": execution_plan_decision.mode,
                "reason": execution_plan_decision.reason,
                "rejections": dict(execution_plan_decision.rejections),
            }

        # SP-05 job-level validators (P5.INFRA.4) + D8 combined quarantine pass;
        # see `_pipeline_finalize.finalize_validators_and_quarantine` for the
        # full "why" (trap T5, LOW-1 raise-before-write ordering, etc). Mutates
        # `quality_metrics` in place and returns the (possibly quarantine-filtered) outputs.
        outputs, quarantine_removed = _pipeline_finalize.finalize_validators_and_quarantine(
            outputs,
            config=ctx.config,
            caller_sources=step.sources,
            mask_row_errors=step.mask_row_errors,
            quality_metrics=quality_metrics,
        )

        # S2: full-frame execution telemetry (the sequential route returned
        # early with its own telemetry).
        quality_metrics["execution"] = _route_exec.execution_telemetry(
            route="full_frame",
            route_reason=decision.route_reason,
            sink=publish.active_sink,
            source_loader=None,
            sources_resident=True,
            inputs_streamed=step.inputs_streamed,
        )
        stamp_out_of_core_declined(quality_metrics, decision.ooc_declined)

        result = ExecutionResult(
            outputs=outputs,
            timings=step.mask_timings,
            boundary_conversion_ms=step.mask_conversion_ms,
            warnings=step.mask_warnings,
            quality_metrics=quality_metrics,
            table_kinds=ctx.table_kinds,
            row_errors=step.mask_row_errors,
        )

        # A1: opt-in post-execution scan suite. Runs only on this full-frame finalize
        # branch -- routing declined the sequential / out-of-core / unified-slice
        # early returns for an opted-in job, so this is the one seam it reaches.
        # Default-OFF returns before touching the result (byte-identical).
        _pipeline_finalize.compute_post_validation(
            result,
            plan=ctx.plan,
            sources=step.sources,
            quarantine_row_mask=quarantine_removed,
            profile=ctx.profile,
            registry=ctx.registry,
            relationship_graph=ctx.graph,
            namespace_registry=ctx.namespace_registry,
            post_validation=ctx.post_validation,
            post_validation_skip=ctx.post_validation_skip,
            post_validation_sample_size=ctx.post_validation_sample_size,
            post_validation_enforce=ctx.post_validation_enforce,
        )
        publish.commit()  # B6a: the run's last action; inert unless this run streamed
        return result

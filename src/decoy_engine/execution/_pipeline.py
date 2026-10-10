"""FC-1 (2026-06-02) unified pipeline entry: mixed mask + generate.

The single load-bearing function this module exposes is `run_pipeline`.
It is the V2 spine the platform job runner + the CLI both call when
the operator submits a `PipelineConfig` that may declare BOTH mask-kind
tables (with `columns:`) AND generate-kind tables (with
`generate_columns:` + `row_count:`) in a single config.

Sequencing contract (PO directive 2026-06-01 + FC-1 spec):

  1. Validate-by-precondition: the caller has already run
     `PipelineConfig.model_validate(raw).model_dump()` and is handing in
     the resulting dict; this entry does not re-validate.
  2. `profile_source(config)` runs over the declared `sources:` block.
     Pure-generate configs (empty `sources:`) get a zero-table Profile.
  3. `compile_plan(config, profile, decoy_engine_version=...)` produces
     the frozen Plan that covers every table in `tables:`. The compiler
     already handles per-table-kind (S6-ENG-1 wired generate into the
     plan compile path).
  4. `build_namespace_registry` + `check_orphan_fk_policy_completeness`
     + `build_relationship_graph` run as usual; the FK graph spans both
     kinds (a generate table can be referenced by a mask child and
     vice versa post-FC-1).
  5. Decide the execution route (`_pipeline_routing.decide_execution_route`):
     a relationship-bearing pure-mask job takes the bounded-memory
     sequential path (early return); everything else continues below.
  6. Split `tables:` into generate-kind (have `generate_columns`) and
     mask-kind (have `columns`). Call `generate_tables(plan, ...)` FIRST
     (Plan-only, guide 4.8/9) so generate outputs exist as Arrow tables.
  7. Merge generate outputs into the `sources` dict the mask adapter
     reads. A mask table whose FK parent is a generate table reads the
     generate output as if it were a source: the generate-side value
     IS the FK pool for the mask side.
  8. Call the selected execution adapter (`select_execution_adapter`;
     default `substrate="pandas"`) to mask the mask-kind tables, unless
     the job auto-routes to the chunked lane (`decide_chunk_route`, then
     `_pipeline_auto_chunk`). The plan only carries mask-table seeds;
     generate tables are not re-traversed.
  9. Build one `ExecutionResult` whose `outputs` covers every output
     table (generate + mask) and whose `table_kinds` dict carries the
     per-table kind for the manifest stamping at F3 / platform side.

Per-table evidence-kind stamping (PO D1 sub-decision 2026-06-01,
RESOLVED per-table): the unified ExecutionResult carries `table_kinds:
dict[str, "mask" | "generate"]` so `update_finished_manifest` writes
`kind="mask"` or `kind="generate"` per table in one manifest.

Out of scope for FC-1 (deferred to V2.1):

- Generate child to mask parent FK direction. The mask parent has a
  finite pre-existing pool; resolving generate children against it
  crosses the generate `reference` generator into the mask substrate.
  REJECTED at schema validation post-2026-06-02 (engine FC-1 QA
  review Finding 2): `_reference_graph_valid` raises at submit time
  when a generate column's `reference_table` points at a mask-kind
  parent. Operators see a clear "deferred to V2.1" error up front
  instead of a hung job at runtime.
- Non-pandas substrates. Pandas is the only masking substrate. `run_pipeline`
  defaults its `substrate` knob to `"pandas"` (also the `DECOY_SUBSTRATE`
  default); any other value raises `invalid_substrate`. The knob and the
  adapter seam are retained as fail-closed defence for a future substrate.
- Per-node preview on mixed configs. Covered by F5 at the platform
  layer (`run_v2_pipeline_preview`).

Execution routing (S2 relationship routing + S3 auto-chunk routing) is
documented in full on `_pipeline_routing` -- that module owns the
decision logic; this module only calls it in the fixed order the module
docstring describes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa

from decoy_engine.execution import _pipeline_context as _ctx
from decoy_engine.execution import (
    _pipeline_finalize,
    _pipeline_generate_mask,
)
from decoy_engine.execution import _pipeline_route_dispatch as _dispatch
from decoy_engine.execution import _pipeline_route_exec as _route_exec
from decoy_engine.execution import _pipeline_sources as _psrc
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._pipeline_context import classify_table_kinds
from decoy_engine.execution._planner import (
    FULL_FRAME_REJECT_ROWS_DEFAULT,
    OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
)
from decoy_engine.execution._stitch import stitch_generate_mask_outputs
from decoy_engine.execution._transforms_admission import stamp_out_of_core_declined
from decoy_engine.execution._transforms_prepare import prepare_transform_sources
from decoy_engine.execution._unified_slice import run_from_pipeline_locals
from decoy_engine.generation.pool import PoolCache
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from faker import Faker

    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.providers_v2 import ProviderRegistry


__all__ = ["classify_table_kinds", "run_pipeline"]

# The substrate/auto-chunk knob defaults live on `_pipeline_finalize` now
# (single source of truth for both this signature and the non-default-ness
# check `stamp_execution_metrics` computes); referenced here via the
# already-imported module so this file does not re-declare them.
# SC2 out-of-core auto-routing thresholds (per largest mask table). Defaults
# target the 32 GB deployment box; see `_planner` for the memory-model
# reasoning. Plumbed as run_pipeline kwargs so the platform SC5 estimator can
# override them with box+schema-calibrated values.
_OUT_OF_CORE_THRESHOLD_DEFAULT = OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT
_FULL_FRAME_REJECT_DEFAULT = FULL_FRAME_REJECT_ROWS_DEFAULT


def run_pipeline(
    config: dict[str, Any],
    sources: Mapping[str, pa.Table | LazySource] | None = None,
    *,
    engine_version: str,
    registry: ProviderRegistry | None = None,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
    vault_writer: Any = None,
    fidelity_report: bool = False,
    post_validation: bool = False,
    post_validation_skip: list[str] | None = None,
    post_validation_sample_size: int = 100,
    post_validation_enforce: bool = False,
    now_iso: str | None = None,
    execution_mode: Literal["auto", "sequential", "full_frame", "out_of_core"] = "auto",
    sink: TransactionalSink | None = None,
    source_loader: Callable[[str], pa.Table] | None = None,
    substrate: str | None = _pipeline_finalize.SUBSTRATE_DEFAULT,
    fpe_chunk_count: int = _pipeline_finalize.FPE_CHUNK_COUNT_DEFAULT,
    max_workers: int = _pipeline_finalize.MAX_WORKERS_DEFAULT,
    fallback_to_pandas: bool = _pipeline_finalize.FALLBACK_TO_PANDAS_DEFAULT,
    explain_plan: bool = False,
    auto_chunk: bool = _pipeline_finalize.AUTO_CHUNK_DEFAULT,
    chunk_size_rows: int = _pipeline_finalize.CHUNK_SIZE_ROWS_DEFAULT,
    auto_chunk_threshold_rows: int = _pipeline_finalize.AUTO_CHUNK_THRESHOLD_DEFAULT,
    native_threads: int = 1,
    chunked_dispatcher_enabled: bool = True,
    stream_chunked_output: bool = True,
    multi_table_dispatch_enabled: bool = True,
    out_of_core_threshold_rows: int = _OUT_OF_CORE_THRESHOLD_DEFAULT,
    full_frame_reject_rows: int = _FULL_FRAME_REJECT_DEFAULT,
    out_of_core_budget_bytes: int | None = None,
    use_byte_estimate_routing: bool = True,
    use_probe_routing: bool = True,
    key_provider: KeyProvider | None = None,
    out_of_core_reorder_threshold_rows: int | None = None,
    unified_slice_enabled: bool = True,
    _provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> ExecutionResult:
    """Execute a mixed mask + generate config end-to-end.

    `config` MUST be the validated dump from `PipelineConfig.model_validate`;
    no re-validation here. `sources` is the caller-loaded
    `Mapping[table_name -> pa.Table | LazySource]` for the mask-kind tables;
    pure-generate configs may pass `None` (or an empty dict). A `LazySource`
    entry is admitted to auto-chunk and the B7 split from its Parquet footer facts and
    streams when the output streams (a `TransactionalSink`, `_chunked_input`); otherwise
    it is resolved per-route -- see `_pipeline_sources`.
    `engine_version` flows into `compile_plan`'s audit-evidence stamping.

    Returns one `ExecutionResult` whose `outputs` covers every output
    table (generate + mask) and whose `table_kinds` field carries the
    per-table classification for the manifest stamping.

    BF1 (2026-06-26) distribution-fidelity surfacing. `fidelity_report`
    is the opt-in, default-OFF switch that attaches a per-mask-table
    `quality-report/v1` block under `ExecutionResult.quality_metrics`
    (key `fidelity_reports`). It is REPORT-ONLY: a low score never fails
    the job. Default-OFF leaves the hot path byte-for-byte unchanged, so
    golden / compat-corpus fixtures do not move. `now_iso` pins the
    report's `generated_at` for deterministic stamping (None -> wall
    clock). SECURITY: only the assembled, aggregate-only report is
    emitted; the intermediate snapshots (which carry category labels /
    raw values) are never attached. First slice is mask-kind tables,
    marginal-only (no joint_columns); generate-kind tables are skipped.

    A1 (2026-09-23) post-execution validation surfacing. `post_validation` is
    the opt-in, default-OFF switch that runs the post-execution scan suite
    (`validation.post.PostValidationRunner`) over the masked output and attaches
    a `quality_summary` manifest block under `ExecutionResult.quality_metrics`,
    mirroring `fidelity_report`'s default-OFF, report-shaped contract. Default-OFF
    leaves the hot path byte-for-byte unchanged (no `quality_summary`,
    `failed_checks`, or `post_validation_enforce` key), so golden / compat-corpus
    fixtures do not move; these are runtime kwargs, not `config` fields, so they
    never feed `pipeline_config_hash`. `post_validation_skip` names scans to skip;
    `post_validation_sample_size` caps the per-column `sampled_values` evidence
    (default 100). The suite needs the full frame resident, so an opted-in job is
    declined from the sequential / out-of-core routes (same as `fidelity_report`)
    and runs full-frame, or is fail-closed-rejected when too large -- it is never
    silently sent out-of-core where the checks cannot run. Job outcome is
    warn-only by default: a hard-failed scan is recorded in
    `quality_metrics["failed_checks"]` but the engine never fails the job.
    `post_validation_enforce` is emitted as
    `quality_metrics["post_validation_enforce"]` for the platform consumer to
    promote hard-fails to a job failure. SECURITY: only synthetic masked values
    reach the summary (R18); no source PII is emitted.

    Execution routing (`execution_mode`, `sink`, `source_loader`, `auto_chunk`,
    `chunk_size_rows`, `auto_chunk_threshold_rows`, `native_threads`,
    `chunked_dispatcher_enabled`, `multi_table_dispatch_enabled`, `out_of_core_threshold_rows`,
    `full_frame_reject_rows`, `out_of_core_budget_bytes`, `explain_plan`) is
    documented in full on `_pipeline_routing` (the decisions, in a fixed order:
    relationship routing with SC2's fail-closed reject-before-read, then
    auto-chunk routing) and `_pipeline_auto_chunk` (the chunked lane: `native_threads`
    is its kernel thread budget, `chunked_dispatcher_enabled=False` its kill switch;
    `multi_table_dispatch_enabled=False` keeps multi-table jobs whole; with a `sink` the lane
    and a fully dispatched split stream into it, `stream_chunked_output=False` opts out, see
    `_chunked_output_sink`). They are runtime kwargs, never `config` fields: resource policies
    stay out of the profile-hashed, frozen-surface data contract. Masked values are route-neutral; on the
    auto-chunk route schema, types, nullability and metadata follow guarantee 3
    of docs/plans/2026-10-01-dispatcher-auto-chunk.md. The SC2 size thresholds
    default to 32 GB-box-calibrated constants (see `_planner`) and are kwargs the
    platform admission estimator can override; `execution_mode` gains
    `"out_of_core"` as an explicit fail-closed force.

    Sprint B2 (docs/plans/2026-07-10-oom-avoidance-routing-redesign.md
    §3.3/§11/§13): `use_probe_routing` (TB-5 default `True`; no effect without
    `use_byte_estimate_routing=True`, also default `True`; force either `False`
    to roll back) is the two-point micro-probe's fast-path RECOVERY for a job
    the static estimate over-downgrades. See `decide_execution_route` and
    `_pipeline_routing_signals.resolve_probe_recovery`.

    Execution-substrate knobs (mask-kind tables only; generate tables
    always run the synthesize path; the sequential route is pandas-only
    regardless of this knob -- see `_pipeline_routing`):

    - `substrate`: which execution adapter masks the mask-kind tables.
      Default `"pandas"` keeps the original hardcoded pandas route
      byte-identical; pandas is the only substrate, so any other value
      raises `invalid_substrate`. `None` defers to the `DECOY_SUBSTRATE`
      env contract.
    - `fpe_chunk_count`: FPE per-value chunk parallelism.
    - `max_workers` / `fallback_to_pandas`: reserved no-op knobs, passed
      through untouched; the pandas adapter ignores them.

    All four forward to `select_execution_adapter`, which validates them
    up front: an unknown substrate raises `ExecutionError`
    (``code='invalid_substrate'``), a non-positive-int count knob raises
    ``code='invalid_execution_knob'``, both BEFORE any profiling or plan
    compilation. When any knob is non-default the selected adapter
    identity and every knob value are stamped under
    ``quality_metrics["execution_adapter"]`` so a job's performance mode
    is reproducible from its manifest; the all-default path stamps
    nothing, keeping golden fixtures byte-identical.

    `unified_slice_enabled` (default True since 2026-09-20; admission is the safety
    gate, pass False for legacy): see `_unified_slice.maybe_run_unified_slice`.

    `_provider_snapshot` (5a-faker) is a private, test/harness-only hook: an
    already-captured immutable custom-faker-provider view
    (`internal.faker_setup.snapshot_custom_faker_providers`) forwarded straight to
    the `generate_tables` call below, so the shadow-parity harness can pin this
    oracle call to the SAME registry snapshot as its own coordinator-side call.
    `None` (every real caller) resolves against the live registry.
    """
    ctx = _ctx.build_run_context(
        config,
        sources,
        engine_version=engine_version,
        registry=registry,
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
        vault_writer=vault_writer,
        fidelity_report=fidelity_report,
        post_validation=post_validation,
        post_validation_skip=post_validation_skip,
        post_validation_sample_size=post_validation_sample_size,
        post_validation_enforce=post_validation_enforce,
        now_iso=now_iso,
        execution_mode=execution_mode,
        sink=sink,
        source_loader=source_loader,
        substrate=substrate,
        fpe_chunk_count=fpe_chunk_count,
        max_workers=max_workers,
        fallback_to_pandas=fallback_to_pandas,
        explain_plan=explain_plan,
        auto_chunk=auto_chunk,
        chunk_size_rows=chunk_size_rows,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
        native_threads=native_threads,
        chunked_dispatcher_enabled=chunked_dispatcher_enabled,
        stream_chunked_output=stream_chunked_output,
        multi_table_dispatch_enabled=multi_table_dispatch_enabled,
        out_of_core_threshold_rows=out_of_core_threshold_rows,
        full_frame_reject_rows=full_frame_reject_rows,
        out_of_core_budget_bytes=out_of_core_budget_bytes,
        use_byte_estimate_routing=use_byte_estimate_routing,
        use_probe_routing=use_probe_routing,
        key_provider=key_provider,
        out_of_core_reorder_threshold_rows=out_of_core_reorder_threshold_rows,
        unified_slice_enabled=unified_slice_enabled,
        provider_snapshot=_provider_snapshot,
        prepare_sources=prepare_transform_sources,
    )
    decision = _ctx.decide_route(ctx)

    # Layer-1 dispatch: every route is an executor over the same (ctx, decision).
    if ctx.has_mask_table and decision.route == "sequential":
        return _dispatch.execute_sequential_route(ctx, decision)
    if ctx.has_mask_table and decision.route == "out_of_core":
        return _dispatch.execute_out_of_core_route(ctx, decision)

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
        _ctx.unified_slice_locals(ctx, decision, caller_sources, pool_cache)
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

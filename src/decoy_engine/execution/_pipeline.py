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
     the job auto-routes to the chunked entrypoint
     (`_pipeline_routing.decide_chunk_route` /
     `_pipeline_route_exec.run_mask_chunked`). The plan only carries
     mask-table seeds; generate tables are not re-traversed.
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

from decoy_engine.execution import (
    _pipeline_finalize,
    _pipeline_generate_mask,
    _pipeline_routing,
)
from decoy_engine.execution import _pipeline_route_exec as _route_exec
from decoy_engine.execution import _pipeline_sources as _psrc
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._planner import (
    FULL_FRAME_REJECT_ROWS_DEFAULT,
    OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
)
from decoy_engine.execution._stitch import stitch_generate_mask_outputs
from decoy_engine.execution._unified_slice import run_from_pipeline_locals
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
    entry (TB-1) is resolved per-route -- see `_pipeline_sources`.
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

    Execution routing (`execution_mode`, `sink`, `source_loader`,
    `auto_chunk`, `chunk_size_rows`, `auto_chunk_threshold_rows`,
    `out_of_core_threshold_rows`, `full_frame_reject_rows`,
    `out_of_core_budget_bytes`, `explain_plan`) is documented in full on
    `_pipeline_routing`, which owns the decisions this function calls in a
    fixed order: relationship routing (out-of-core vs. sequential vs.
    full_frame, with a fail-closed reject-before-read for a too-big FK job
    no bounded route can take -- SC2) first, then single-table auto-chunk
    routing (chunked vs. full_frame). `execution_mode` / `auto_chunk` are
    resource policies of the invocation, not properties of the data
    transformation, so they are runtime kwargs (matching `vault_writer` /
    `fidelity_report` / `now_iso`), never `config` fields -- they must
    stay out of the profile-hashed, frozen-surface data contract. Every
    route is byte-output-neutral versus full_frame (only peak memory /
    adapter identity differs). The SC2 size thresholds default to
    32 GB-box-calibrated constants (see `_planner`) and are kwargs so the
    platform admission estimator can override them; `execution_mode` gains
    `"out_of_core"` as an explicit fail-closed force.

    Sprint B2 (docs/plans/2026-07-10-oom-avoidance-routing-redesign.md
    §3.3/§11/§13): `use_probe_routing` (TB-5 default `True`, composes with --
    has NO effect without -- `use_byte_estimate_routing=True`, also default
    `True` since TB-5; force either `False` to roll back) is the two-point
    micro-probe's fast-path RECOVERY for a job the static estimate
    over-downgrades. See `_pipeline_routing.decide_execution_route` and
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

    `_provider_snapshot` (5a-faker) is a private, keyword-only, test/harness-
    only hook: an already-captured immutable custom-faker-provider view
    (`internal.faker_setup.snapshot_custom_faker_providers`) forwarded
    straight to the `generate_tables` call below. It exists so the shadow-
    parity harness can pin this oracle call to the SAME registry snapshot
    its own coordinator-side `generate_tables` call reads, instead of two
    live reads a concurrent register/unregister could straddle. `None`
    (the default, every real caller) resolves against the live registry
    exactly as before this parameter existed -- ordinary callers never pass
    it.
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

    # None-normalize the skip list here (a mutable [] default would be shared
    # across calls); the unified-slice `locals()` forwarding reads the bound
    # `post_validation` name directly.
    post_validation_skip = list(post_validation_skip) if post_validation_skip else []

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

    # Routing layer 1 (S2 + SC2): relationship-bearing pure-mask jobs take a
    # bounded-memory route (out-of-core when large + compatible, else
    # sequential); a large FK job no bounded route can take is rejected before
    # read. This is an early return / a fail-closed raise. The SC2 admission +
    # size signals are inert (False/None/None/True) off the relationship+mask
    # shape, so non-FK jobs keep the pre-SC2 routing. The size signal now comes
    # from the (SC7a bounded) profile metadata, so the gates fire on the lazy
    # `source_loader` path too (SC7b, closing the F2 reject-before-read hole).
    route, route_reason = _pipeline_routing.resolve_execution_route(
        profile,
        plan=plan,
        registry=resolved_registry,
        graph=graph,
        caller_sources=caller_sources,
        table_kinds=table_kinds,
        has_mask_table=has_mask_table,
        has_generate_table=has_generate_table,
        validators=(config.get("validators") or []),
        fidelity_report=fidelity_report,
        post_validation=post_validation,
        vault_writer=vault_writer,
        execution_mode=execution_mode,
        resolved_substrate=resolved_substrate,
        out_of_core_threshold_rows=out_of_core_threshold_rows,
        full_frame_reject_rows=full_frame_reject_rows,
        out_of_core_budget_bytes=out_of_core_budget_bytes,
        use_byte_estimate_routing=use_byte_estimate_routing,
        use_probe_routing=use_probe_routing,
        config=config,
        engine_version=engine_version,
    )

    # Routing layer 2 (S3 auto-chunk) classification. Computed BEFORE the
    # layer-1 early return (not just on the full_frame side) so
    # `explain_plan=True` surfaces a classification for EVERY route,
    # including relationship-route-deferred FK jobs that go sequential --
    # `classify_job` is a static plan/config read (no per-row execution
    # work), so computing it here costs nothing beyond that read even when
    # `route_chunked` ends up unused (the sequential branch below ignores
    # it; only the full_frame continuation further down actually consults
    # it for real routing). See `_pipeline_routing` module docstring for
    # the full two-layer composition.
    execution_plan_decision, route_chunked = _pipeline_routing.decide_chunk_route(
        config,
        plan=plan,
        registry=resolved_registry,
        graph=graph,
        substrate=resolved_substrate,
        caller_sources=caller_sources,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
        explain_plan=explain_plan,
        auto_chunk=auto_chunk,
        has_mask_table=has_mask_table,
    )

    if has_mask_table and route == "sequential":
        loader = _psrc.resolve_sequential_loader(source_loader, caller_sources)
        return _route_exec.run_sequential_route(
            plan=plan,
            loader=loader,
            registry=resolved_registry,
            graph=graph,
            namespace_registry=ns_registry,
            sink=sink,
            quarantine_config=config.get("quarantine"),
            route_reason=route_reason,
            source_loader=source_loader,
            sources_resident=bool(caller_sources),
            fpe_chunk_count=fpe_chunk_count,
            table_kinds=table_kinds,
            explain_plan=explain_plan,
            execution_plan_decision=execution_plan_decision,
            unconfigured_column_policy=projection_policy,
            key_provider=resolved_key_provider,
        )

    # SC2 out-of-core route (same shape as sequential); caller_sources feeds
    # the runner directly -- TB-1: a LazySource streams natively here, no materialization.
    if has_mask_table and route == "out_of_core":
        return _route_exec.run_out_of_core_route(
            plan=plan,
            sources=caller_sources,
            registry=resolved_registry,
            graph=graph,
            sink=sink,
            route_reason=route_reason,
            table_kinds=table_kinds,
            source_loader=source_loader,
            sources_resident=bool(caller_sources),
            budget_bytes=out_of_core_budget_bytes,
            explain_plan=explain_plan,
            execution_plan_decision=execution_plan_decision,
            unconfigured_column_policy=projection_policy,
            key_provider=resolved_key_provider,
            out_of_core_reorder_threshold_rows=out_of_core_reorder_threshold_rows,
        )

    unified_slice_result = run_from_pipeline_locals(locals())  # Task 4.5, see its docstring
    if unified_slice_result is not None:
        return unified_slice_result

    # TB-1: only full_frame / auto-chunk below needs every source resident. A
    # loader-backed job (empty `caller_sources` + a `source_loader`) diverted here
    # -- e.g. `post_validation` declined its bounded route -- must still get its
    # real mask-table sources through the loader, never silently empty outputs.
    resident_sources: dict[str, pa.Table] = _psrc.resolve_resident_sources(
        caller_sources,
        source_loader=source_loader,
        required_tables=[name for name, kind in table_kinds.items() if kind == "mask"],
    )

    # Steps 1-2 (generate-kind tables, then mask-kind tables): split into
    # `_pipeline_generate_mask.run_generate_and_mask_steps` to hold this
    # module's own LOC ceiling (see that module's docstring). Pure call
    # extraction; the sequencing/merge/stamp logic is unchanged.
    step_result = _pipeline_generate_mask.run_generate_and_mask_steps(
        has_generate_table=has_generate_table,
        has_mask_table=has_mask_table,
        plan=plan,
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
        provider_snapshot=_provider_snapshot,
        resident_sources=resident_sources,
        route_chunked=route_chunked,
        table_kinds=table_kinds,
        config=config,
        engine_version=engine_version,
        registry=resolved_registry,
        adapter=adapter,
        vault_writer=vault_writer,
        chunk_size_rows=chunk_size_rows,
        key_provider=resolved_key_provider,
        graph=graph,
        namespace_registry=ns_registry,
        unconfigured_column_policy=projection_policy,
        generate_output_tables=generate_output_tables,
        substrate=substrate,
        resolved_substrate=resolved_substrate,
        fpe_chunk_count=fpe_chunk_count,
        max_workers=max_workers,
        fallback_to_pandas=fallback_to_pandas,
        auto_chunk=auto_chunk,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
        execution_plan_decision=execution_plan_decision,
        fidelity_report=fidelity_report,
        now_iso=now_iso,
    )
    generate_outputs = step_result.generate_outputs
    mask_outputs = step_result.mask_outputs
    mask_timings = step_result.mask_timings
    mask_conversion_ms = step_result.mask_conversion_ms
    mask_warnings = step_result.mask_warnings
    mask_quality_metrics = step_result.mask_quality_metrics
    mask_row_errors = step_result.mask_row_errors
    fidelity_reports = step_result.fidelity_reports

    # Step 3: stitch the outputs together via the shared helper both this
    # oracle and the shadow coordinator's mixed dispatch call, so "mask wins
    # ties" cannot drift between the two (Task 4.6 slice 5b-i).
    outputs: dict[str, pa.Table] = stitch_generate_mask_outputs(generate_outputs, mask_outputs)

    # BF1: namespace the fidelity reports under the existing free-form
    # quality_metrics dict (already plumbed to the platform manifest).
    # Additive + default-OFF: when the flag is off, fidelity_reports is
    # empty and quality_metrics is untouched.
    quality_metrics: dict[str, Any] = dict(mask_quality_metrics)
    if fidelity_reports:
        quality_metrics["fidelity_reports"] = fidelity_reports

    # Explain surfacing: stamp the SAME classification the routing decision
    # used (computed once above), so the explain block and the executed
    # route cannot drift apart. Behind the default-off flag; default runs stamp nothing here.
    if explain_plan and execution_plan_decision is not None:
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
        config=config,
        caller_sources=resident_sources,
        mask_row_errors=mask_row_errors,
        quality_metrics=quality_metrics,
    )

    # S2: full-frame execution telemetry (the sequential route returned
    # early above with its own telemetry).
    quality_metrics["execution"] = _route_exec.execution_telemetry(
        route="full_frame",
        route_reason=route_reason,
        sink=None,
        source_loader=None,
        sources_resident=True,
    )

    result = ExecutionResult(
        outputs=outputs,
        timings=mask_timings,
        boundary_conversion_ms=mask_conversion_ms,
        warnings=mask_warnings,
        quality_metrics=quality_metrics,
        table_kinds=table_kinds,
        row_errors=mask_row_errors,
    )

    # A1: opt-in post-execution scan suite. Runs only on this full-frame finalize
    # branch -- routing declined the sequential / out-of-core / unified-slice
    # early returns for an opted-in job, so this is the one seam it reaches.
    # Default-OFF returns before touching the result (byte-identical).
    _pipeline_finalize.compute_post_validation(
        result,
        plan=plan,
        sources=resident_sources,
        quarantine_row_mask=quarantine_removed,
        profile=profile,
        registry=resolved_registry,
        relationship_graph=graph,
        namespace_registry=ns_registry,
        post_validation=post_validation,
        post_validation_skip=post_validation_skip,
        post_validation_sample_size=post_validation_sample_size,
        post_validation_enforce=post_validation_enforce,
    )
    return result

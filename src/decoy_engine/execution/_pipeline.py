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
     mask-kind (have `columns`). Call `generate_tables(config, ...)`
     FIRST so generate outputs exist as Arrow tables.
  7. Merge generate outputs into the `sources` dict the mask adapter
     reads. A mask table whose FK parent is a generate table reads the
     generate output as if it were a source: the generate-side value
     IS the FK pool for the mask side.
  8. Call the selected execution adapter (`select_execution_adapter`;
     default `substrate="pandas"`) to mask the mask-kind tables, unless
     the job auto-routes to the chunked entrypoint
     (`_pipeline_routing.decide_chunk_route` / `run_mask_chunked`). The
     plan only carries mask-table seeds; generate tables are not
     re-traversed.
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
- Cross-substrate mixed mode. Polars falls back to pandas for FK paths
  (`_polars_adapter.py:121`); the pandas adapter is the canonical
  mixed-mode adapter for V2 ship. `run_pipeline` therefore defaults its
  `substrate` knob to `"pandas"` rather than inheriting the S13
  DECOY_SUBSTRATE default flip; polars is an explicit per-call opt-in
  (`substrate="polars"` or `substrate=None` to honor the env var).
- Per-node preview on mixed configs. Covered by F5 at the platform
  layer (`run_v2_pipeline_preview`).

Execution routing (S2 relationship routing + S3 auto-chunk routing) is
documented in full on `_pipeline_routing` -- that module owns the
decision logic; this module only calls it in the fixed order the module
docstring describes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa

from decoy_engine.execution import _pipeline_routing
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._planner import (
    AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT,
    FULL_FRAME_REJECT_ROWS_DEFAULT,
    OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
)

if TYPE_CHECKING:
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.providers_v2 import ProviderRegistry


__all__ = ["classify_table_kinds", "run_pipeline"]

# run_pipeline's execution-knob defaults. `substrate` pins "pandas" (NOT
# None): resolve_substrate(None) follows DECOY_SUBSTRATE and its S13
# default flip to polars, and run_pipeline's default route must stay
# byte-identical to the original hardcoded pandas path. The signature
# defaults and the non-default metadata stamp both read from here so
# they cannot drift.
_SUBSTRATE_DEFAULT = "pandas"
_FPE_CHUNK_COUNT_DEFAULT = 4
_MAX_WORKERS_DEFAULT = 4
_FALLBACK_TO_PANDAS_DEFAULT = True
# Auto-chunk defaults. Default-ON is safe because identity is enforced
# twice: the planner's fail-closed gates admit only jobs whose every
# per-column output is a pure function of (value, config, seed) with all
# whole-column inputs pinned (date_shift needs an explicit date_format,
# bucketize a null-free numeric source, `when` predicates never route),
# and the strict chunk concat refuses to merge chunks whose schemas
# disagree (a gate miss raises rather than silently promoting); the
# fixture matrix in tests/unit/execution/test_auto_chunk_routing.py is
# regression evidence for that contract, not its proof. 50k-row chunks
# bound the per-chunk pandas working set at negligible per-chunk
# plan/adapter overhead (P0 showed wall-clock parity at 10k rows).
_AUTO_CHUNK_DEFAULT = True
_CHUNK_SIZE_ROWS_DEFAULT = 50_000
_AUTO_CHUNK_THRESHOLD_DEFAULT = AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT
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
    sources: dict[str, pa.Table] | None = None,
    *,
    engine_version: str,
    registry: ProviderRegistry | None = None,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
    vault_writer: Any = None,
    fidelity_report: bool = False,
    now_iso: str | None = None,
    execution_mode: Literal["auto", "sequential", "full_frame", "out_of_core"] = "auto",
    sink: TransactionalSink | None = None,
    source_loader: Callable[[str], pa.Table] | None = None,
    substrate: str | None = _SUBSTRATE_DEFAULT,
    fpe_chunk_count: int = _FPE_CHUNK_COUNT_DEFAULT,
    max_workers: int = _MAX_WORKERS_DEFAULT,
    fallback_to_pandas: bool = _FALLBACK_TO_PANDAS_DEFAULT,
    explain_plan: bool = False,
    auto_chunk: bool = _AUTO_CHUNK_DEFAULT,
    chunk_size_rows: int = _CHUNK_SIZE_ROWS_DEFAULT,
    auto_chunk_threshold_rows: int = _AUTO_CHUNK_THRESHOLD_DEFAULT,
    out_of_core_threshold_rows: int = _OUT_OF_CORE_THRESHOLD_DEFAULT,
    full_frame_reject_rows: int = _FULL_FRAME_REJECT_DEFAULT,
    out_of_core_budget_bytes: int | None = None,
) -> ExecutionResult:
    """Execute a mixed mask + generate config end-to-end.

    `config` MUST be the validated dump from `PipelineConfig.model_validate`;
    no re-validation here. `sources` is the caller-loaded
    `dict[table_name -> pa.Table]` for the mask-kind tables; pure-generate
    configs may pass `None` (or an empty dict). `engine_version` flows
    into `compile_plan`'s audit-evidence stamping.

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

    Execution-substrate knobs (mask-kind tables only; generate tables
    always run the synthesize path; the sequential route is pandas-only
    regardless of this knob -- see `_pipeline_routing`):

    - `substrate`: which execution adapter masks the mask-kind tables.
      Default `"pandas"` keeps the original hardcoded pandas route
      byte-identical; `"polars"` opts a scalar no-FK job into the
      polars-native route (FK/composite work still falls back to the
      pandas oracle exactly as `PolarsExecutionAdapter` dictates);
      `None` defers to the `DECOY_SUBSTRATE` env contract.
    - `fpe_chunk_count`: FPE per-value chunk parallelism (both adapters).
    - `max_workers` / `fallback_to_pandas`: polars-adapter knobs, passed
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
    """
    from decoy_engine.execution._substrate import (
        require_bool,
        require_positive_int,
        resolve_substrate,
        select_execution_adapter,
    )
    from decoy_engine.generation.synthesize import generate_tables
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

    resolved_registry = registry if registry is not None else get_default_registry()
    caller_sources: dict[str, pa.Table] = dict(sources) if sources else {}

    table_kinds = classify_table_kinds(config)
    has_mask_table = any(kind == "mask" for kind in table_kinds.values())
    has_generate_table = any(kind == "generate" for kind in table_kinds.values())

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
    # size signals are inert (False/None/None) off the relationship+mask shape,
    # so non-FK jobs keep the pre-SC2 routing.
    out_of_core_compatible, out_of_core_reject_code, largest_table_rows = (
        _pipeline_routing.out_of_core_routing_signals(
            profile,
            plan=plan,
            registry=resolved_registry,
            graph=graph,
            caller_sources=caller_sources,
            table_kinds=table_kinds,
            has_mask_table=has_mask_table,
        )
    )
    route, route_reason = _pipeline_routing.decide_execution_route(
        profile,
        has_generate_table=has_generate_table,
        has_mask_table=has_mask_table,
        validators=(config.get("validators") or []),
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
        execution_mode=execution_mode,
        graph=graph,
        resolved_substrate=resolved_substrate,
        out_of_core_compatible=out_of_core_compatible,
        out_of_core_reject_code=out_of_core_reject_code,
        largest_table_rows=largest_table_rows,
        out_of_core_threshold_rows=out_of_core_threshold_rows,
        full_frame_reject_rows=full_frame_reject_rows,
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
        loader = source_loader if source_loader is not None else (lambda t: caller_sources[t])
        return _pipeline_routing.run_sequential_route(
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
        )

    # SC2: the bounded-RAM out-of-core FK route (same early-return shape as the
    # sequential branch). The resident caller_sources feed the runner directly
    # (the pure-mask FK shape has every parent + child present); a sink streams.
    if has_mask_table and route == "out_of_core":
        return _pipeline_routing.run_out_of_core_route(
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
        )

    # Step 1: generate-kind tables. The synthesize entry filters by
    # `generate_columns` presence already (synthesize.py:113), so passing
    # the full config is safe even when mask tables are present.
    generate_outputs: dict[str, pa.Table] = {}
    if has_generate_table:
        generate_outputs = generate_tables(
            config,
            derive_key=derive_key,
            instance_default_locale=instance_default_locale,
        )

    # Step 2: mask-kind tables.
    mask_outputs: dict[str, pa.Table] = {}
    mask_timings: tuple = ()
    mask_conversion_ms: float = 0.0
    mask_warnings: tuple = ()
    mask_quality_metrics: dict[str, Any] = {}
    fidelity_reports: dict[str, Any] = {}
    # Honesty pack (D7/D8): populated from `mask_result.row_errors` on the
    # full-frame branch below. The chunked branch leaves this `()` by
    # construction -- see `_pipeline_routing.run_mask_chunked`'s docstring:
    # a routed job that reaches this point is never eligible for row-error
    # quarantine (same policy the manual chunked entrypoint enforces).
    mask_row_errors: tuple[Any, ...] = ()
    if has_mask_table:
        # Merge generate outputs into the sources dict the mask adapter
        # reads. A mask table whose FK parent is a generate table reads the
        # generate output as if it were a source: the generated value IS
        # the FK pool for the mask side.
        merged_sources: dict[str, pa.Table] = {}
        merged_sources.update(caller_sources)
        merged_sources.update(generate_outputs)

        if route_chunked:
            # The eligible shape is exactly one mask table with no generate
            # tables, so merged_sources holds only that table's frame; the
            # planner's runtime gates already rejected anything else.
            mask_table_name = next(name for name, kind in table_kinds.items() if kind == "mask")
            mask_outputs, mask_timings, mask_conversion_ms, mask_warnings = (
                _pipeline_routing.run_mask_chunked(
                    config,
                    merged_sources[mask_table_name],
                    table=mask_table_name,
                    engine_version=engine_version,
                    registry=resolved_registry,
                    adapter=adapter,
                    vault_writer=vault_writer,
                    chunk_size_rows=chunk_size_rows,
                )
            )
        else:
            mask_result = adapter.run(
                plan,
                merged_sources,
                registry=resolved_registry,
                relationship_graph=graph,
                namespace_registry=ns_registry,
            )
            # Adapters echo every source frame in `outputs` (generate-kind
            # entries in `merged_sources` come back round-tripped through the
            # substrate). Keeping them all preserves the established stitch
            # contract below, where mask_result wins ties over the raw
            # generate outputs.
            mask_outputs = dict(mask_result.outputs)
            mask_timings = mask_result.timings
            mask_conversion_ms = mask_result.boundary_conversion_ms
            mask_warnings = mask_result.warnings
            mask_quality_metrics = dict(mask_result.quality_metrics)
            mask_row_errors = mask_result.row_errors
            # Token vault (deferred follow-up 1): collect source->masked pairs
            # for vault: true columns. Opt-in via the kwarg; the caller writes
            # the artifact. The chunked route accumulates the same entries
            # per chunk inside run_mask_pipeline_chunked instead.
            if vault_writer is not None:
                from decoy_engine.vault import collect_vault_entries

                vault_writer.add(collect_vault_entries(config, merged_sources, mask_outputs))
        # Performance-mode reproducibility: any non-default knob stamps
        # the selected adapter identity + every knob value into the job
        # metadata. The all-default path stamps nothing so golden and
        # compat-corpus fixtures stay byte-identical.
        if (substrate, fpe_chunk_count, max_workers, fallback_to_pandas) != (
            _SUBSTRATE_DEFAULT,
            _FPE_CHUNK_COUNT_DEFAULT,
            _MAX_WORKERS_DEFAULT,
            _FALLBACK_TO_PANDAS_DEFAULT,
        ):
            mask_quality_metrics["execution_adapter"] = {
                "adapter_name": adapter.adapter_name,
                "adapter_version": adapter.adapter_version,
                "requested_substrate": substrate,
                "resolved_substrate": resolved_substrate,
                "fpe_chunk_count": fpe_chunk_count,
                "max_workers": max_workers,
                "fallback_to_pandas": fallback_to_pandas,
            }
        # Auto-chunk reproducibility stamp: a routed run is non-default by
        # definition; a non-routed run stamps only when an auto-chunk knob
        # is non-default. All-default full-frame runs stamp nothing (the
        # P1 golden `quality_metrics == {}` contract).
        if route_chunked or (auto_chunk, chunk_size_rows, auto_chunk_threshold_rows) != (
            _AUTO_CHUNK_DEFAULT,
            _CHUNK_SIZE_ROWS_DEFAULT,
            _AUTO_CHUNK_THRESHOLD_DEFAULT,
        ):
            mask_quality_metrics["auto_chunk"] = _pipeline_routing.auto_chunk_stamp(
                route_chunked=route_chunked,
                auto_chunk=auto_chunk,
                chunk_size_rows=chunk_size_rows,
                auto_chunk_threshold_rows=auto_chunk_threshold_rows,
                table_kinds=table_kinds,
                caller_sources=caller_sources,
                decision=execution_plan_decision,
            )

        # BF1: opt-in, report-only distribution fidelity per mask table.
        # Imported lazily so the default-OFF path never pulls in the
        # quality stack. SECURITY: emit ONLY the assembled report
        # (aggregate, label-free); the snapshots that carry raw category
        # values stay inside compute_quality_report and are never attached.
        if fidelity_report:
            from decoy_engine.quality.report import compute_quality_report

            for table_name, out_table in mask_outputs.items():
                if table_kinds.get(table_name) != "mask":
                    continue  # first slice: mask-kind tables only
                src_table = merged_sources.get(table_name)
                if src_table is None:
                    continue
                fidelity_reports[table_name] = compute_quality_report(
                    src_table.to_pandas(),
                    out_table.to_pandas(),
                    expect_row_parity=True,
                    joint_columns=None,
                    now_iso=now_iso,
                )

    # Step 3: stitch the outputs together. Mask wins ties (every name in
    # the config maps to one kind by construction, so no real conflicts).
    outputs: dict[str, pa.Table] = {}
    outputs.update(generate_outputs)
    outputs.update(mask_outputs)

    # BF1: namespace the fidelity reports under the existing free-form
    # quality_metrics dict (already plumbed to the platform manifest).
    # Additive + default-OFF: when the flag is off, fidelity_reports is
    # empty and quality_metrics is untouched.
    quality_metrics: dict[str, Any] = dict(mask_quality_metrics)
    if fidelity_reports:
        quality_metrics["fidelity_reports"] = fidelity_reports

    # Explain surfacing: stamp the SAME classification the routing decision
    # used (computed once above), so the explain block and the executed
    # route cannot drift apart. Behind the default-off flag; default runs
    # stamp nothing here.
    if explain_plan and execution_plan_decision is not None:
        quality_metrics["execution_plan"] = {
            "mode": execution_plan_decision.mode,
            "reason": execution_plan_decision.reason,
            "rejections": dict(execution_plan_decision.rejections),
        }

    # SP-05 (2026-06-27): job-level validator framework (P5.INFRA.4).
    # Runs AFTER all column passes complete, on the UNFILTERED outputs
    # (trap T5). Fail-closed by default: a validator failure raises
    # ValidatorFailedError unless quarantine is enabled with the
    # validation_fail trigger.
    validators_config: list[Any] = config.get("validators") or []
    v_report: Any = None
    if validators_config:
        import dataclasses

        from decoy_engine.validators._registry import validate as _run_validators

        v_report = _run_validators(outputs, config, sources=caller_sources)
        quality_metrics["validation"] = {"validators": dataclasses.asdict(v_report)}

    if mask_row_errors:
        # Additive manifest key, counts only (no cell values -- trap T3).
        row_error_counts: dict[str, int] = {}
        for rec in mask_row_errors:
            key = f"{rec.table}.{rec.column}[{rec.trigger}]"
            row_error_counts[key] = row_error_counts.get(key, 0) + 1
        quality_metrics["row_errors"] = row_error_counts

    validator_failed = v_report is not None and not v_report.passed

    # D8: one combined quarantine pass over validator findings + row errors,
    # then a fail-closed remainder rule for anything quarantine did not
    # cover. Same precedence as before for validation_fail alone; row
    # errors get the identical treatment via their own trigger.
    # Quarantine JSONL is durable only on a successful (fully covered) run; a
    # fail-loud run publishes nothing.
    if validator_failed or mask_row_errors:
        from decoy_engine.errors import RowErrorsFailedError, ValidatorFailedError

        quarantine_cfg: dict[str, Any] = config.get("quarantine") or {}
        q_enabled = bool(quarantine_cfg.get("enabled", False))
        q_triggers: list[str] = list(quarantine_cfg.get("triggers") or [])

        validation_covered = q_enabled and validator_failed and "validation_fail" in q_triggers
        row_errors_covered = tuple(
            r for r in mask_row_errors if q_enabled and r.trigger in q_triggers
        )
        row_errors_uncovered = tuple(r for r in mask_row_errors if r not in row_errors_covered)

        # LOW-1 (S2 remediation guide section 8): raise BEFORE writing the
        # quarantine JSONL, matching the sequential path (which raises before
        # its single post-loop write). A fail-loud run must publish nothing
        # durable, including a partial quarantine JSONL from the covered
        # remainder of a mixed covered+uncovered run.
        if validator_failed and not validation_covered:
            raise ValidatorFailedError(v_report)
        if row_errors_uncovered:
            raise RowErrorsFailedError(row_errors_uncovered)

        if validation_covered or row_errors_covered:
            from decoy_engine.quarantine import apply_quarantine, quarantine_manifest

            outputs, q_summary = apply_quarantine(
                outputs, v_report, quarantine_cfg, row_errors=mask_row_errors
            )
            quality_metrics["quarantine"] = quarantine_manifest(q_summary)

    # S2: full-frame execution telemetry (the sequential route returned
    # early above with its own telemetry).
    quality_metrics["execution"] = _pipeline_routing.execution_telemetry(
        route="full_frame",
        route_reason=route_reason,
        sink=None,
        source_loader=None,
        sources_resident=True,
    )

    return ExecutionResult(
        outputs=outputs,
        timings=mask_timings,
        boundary_conversion_ms=mask_conversion_ms,
        warnings=mask_warnings,
        quality_metrics=quality_metrics,
        table_kinds=table_kinds,
        row_errors=mask_row_errors,
    )

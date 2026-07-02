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
  5. Split `tables:` into generate-kind (have `generate_columns`) and
     mask-kind (have `columns`). Call `generate_tables(config, ...)`
     FIRST so generate outputs exist as Arrow tables.
  6. Merge generate outputs into the `sources` dict the mask adapter
     reads. A mask table whose FK parent is a generate table reads the
     generate output as if it were a source: the generate-side value
     IS the FK pool for the mask side.
  7. Call the selected execution adapter (`select_execution_adapter`;
     default `substrate="pandas"`) to mask the mask-kind tables. The
     plan only carries mask-table seeds; generate tables are not
     re-traversed.
  8. Build one `ExecutionResult` whose `outputs` covers every output
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
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa

from decoy_engine.errors import ConfigError
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution._sequential import run_sequential

if TYPE_CHECKING:
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph


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


def _has_cross_table_fk_cycle(graph: RelationshipGraph) -> bool:
    """True if the table-level FK graph has a cycle across DISTINCT tables.

    Sequential masking orders whole tables (table_topo_order), so a
    cross-table cycle cannot be sequenced; self-edges (self-ref FK) mask
    within one table and are not a table-level cycle (S2 remediation guide
    r3 section 6).
    """
    from collections import defaultdict

    succ: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        if edge.parent_table != edge.child_table:
            succ[edge.parent_table].add(edge.child_table)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for nxt in succ.get(node, ()):  # DFS back-edge = cycle
            c = color.get(nxt, WHITE)
            if c == GRAY or (c == WHITE and visit(nxt)):
                return True
        color[node] = BLACK
        return False

    return any(color.get(n, WHITE) == WHITE and visit(n) for n in list(succ))


def _sequential_eligible(
    profile: Any,
    *,
    has_generate_table: bool,
    validators: list[Any],
    fidelity_report: bool,
    vault_writer: Any,
) -> tuple[bool, str]:
    """Decide whether a mask job may take the bounded-memory sequential path.

    Returns (eligible, reason). `reason` is a stable telemetry token; when
    eligible it is "pure_mask_fk", otherwise it names the disqualifier.

    The sequential path streams/evicts table by table, so any run_pipeline
    post-mask step that needs every masked output resident at once
    disqualifies it: job-level validators (compare positionally against all
    sources), the fidelity report, and the token-vault collection. Pure-generate
    and mixed generate+mask jobs are disqualified because generate tables are
    not masked table-by-table through this path.
    """
    if not profile.relationships:
        return False, "no_relationships"
    if has_generate_table:
        return False, "generate_plus_mask"
    if validators:
        return False, "validators_present"
    if fidelity_report:
        return False, "fidelity_report_requested"
    if vault_writer is not None:
        return False, "vault_writer_requested"
    return True, "pure_mask_fk"


def _execution_telemetry(
    *, route: str, route_reason: str, sink: Any, source_loader: Any, sources_resident: bool
) -> dict[str, Any]:
    """Per-config execution memory telemetry. Honest by construction: it never
    claims bounded input residency unless the caller's Arrow sources are
    actually NOT resident (a lazy source_loader supplied AND no non-empty
    `sources` dict), and never claims streamed outputs unless a sink was
    supplied.

    MEDIUM (S2 remediation guide section 8): `run_pipeline` always builds
    `caller_sources = dict(sources)` (L236-ish), so a non-empty `sources` dict
    means the inputs ARE resident in memory even when a lazy `source_loader`
    is ALSO supplied. `sources_resident` carries that fact in; bounded input
    residency is reported ONLY for the one configuration that actually bounds
    inputs: a lazy loader supplied AND `sources` empty/omitted.
    """
    if route == "full_frame":
        return {
            "execution_mode": "full_frame",
            "route_reason": route_reason,
            "eviction": "none",
            "outputs_streamed": False,
            "loaded_fully_in_memory": True,
        }
    return {
        "execution_mode": "sequential",
        "route_reason": route_reason,
        "eviction": "per_table",
        "outputs_streamed": sink is not None,
        "loaded_fully_in_memory": sources_resident or source_loader is None,
    }


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
    execution_mode: Literal["auto", "sequential", "full_frame"] = "auto",
    sink: TransactionalSink | None = None,
    source_loader: Callable[[str], pa.Table] | None = None,
    substrate: str | None = _SUBSTRATE_DEFAULT,
    fpe_chunk_count: int = _FPE_CHUNK_COUNT_DEFAULT,
    max_workers: int = _MAX_WORKERS_DEFAULT,
    fallback_to_pandas: bool = _FALLBACK_TO_PANDAS_DEFAULT,
    explain_plan: bool = False,
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

    S2 (engine "Finish Open-Ended Surfaces" program) routing: a relationship-
    bearing PURE-MASK job (no generate tables, no validators, no
    fidelity_report, no vault_writer -- see `_sequential_eligible`) is, by
    default (`execution_mode="auto"`), routed through the bounded-memory
    `run_sequential` path instead of the full-frame adapter-selected `run`
    path. `execution_mode="full_frame"` always forces full-frame (even when
    eligible); `execution_mode="sequential"` forces sequential and raises
    `ConfigError` if the job is not eligible (fail-closed: never silently
    ignore an explicit request). `execution_mode` is a resource policy of the
    invocation, not a property of the data transformation, so it is a runtime
    kwarg (matching `vault_writer` / `fidelity_report` / `now_iso`), never a
    `config` field -- it must stay out of the profile-hashed, frozen-surface
    data contract, and sequential vs. full-frame is byte-output-neutral (only
    peak memory differs). `sink` and `source_loader` are passed straight
    through to `run_sequential` when the sequential route is taken; every
    existing caller (which passes neither) gets full-frame (unchanged) or
    sequential-in-memory (same `result.outputs`, byte-identical, lower pandas
    peak) -- the empty-`result.outputs` streamed path is only reachable when a
    `sink` is explicitly passed. Non-FK / mixed / validator jobs are
    byte-identical to today: they take the untouched full-frame branch; the
    only addition is one telemetry key under `quality_metrics["execution"]`.
    The sequential path is pandas-only by construction (`run_sequential`
    is typed to `PandasExecutionAdapter`, matching the existing FK-paths-
    fall-back-to-pandas contract), so it always constructs its own
    `PandasExecutionAdapter()` regardless of the `substrate` knob below;
    `substrate` only ever affects the full-frame mask branch.

    Execution-substrate knobs (mask-kind tables only; generate tables
    always run the synthesize path; the sequential route above is
    unaffected -- pandas-only regardless of this knob):

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

    `explain_plan` (observe-only planner surfacing): when True, the
    execution-mode planner (`classify_job`) classifies the job into one
    of the five execution modes and the classification (chosen mode +
    per-rejected-mode reasons) is stamped under
    ``quality_metrics["execution_plan"]``. The planner NEVER changes
    routing: execution takes exactly the same adapter path either way
    (planner-driven routing sits behind the separate
    ``_planner.PLANNER_ROUTING_ENABLED`` seam, hard False for now).
    Default False stamps nothing and does no classification work, so the
    default route stays byte-identical.
    """
    from decoy_engine.execution._substrate import resolve_substrate, select_execution_adapter
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

    # S2: decide the execution route once, right after the graph is built.
    eligible, route_reason = _sequential_eligible(
        profile,
        has_generate_table=has_generate_table,
        validators=(config.get("validators") or []),
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
    )
    # S2 round 3: a mutual cross-table FK cycle (A -> B -> A) cannot be
    # ordered by table_topo_order, so `auto` must not route it to
    # sequential (it ran fine under full_frame before this program; routing
    # it to sequential is a functional regression, not a leak -- see
    # docs/backlog/s2-fk-leak-remediation-r3-guide.md section 6). A
    # self-referencing table (one table, not a cross-table cycle) is NOT
    # flagged here and still routes normally.
    cyclic = _has_cross_table_fk_cycle(graph)
    route: str
    if execution_mode == "full_frame":
        route = "full_frame"
        route_reason = "override_full_frame"
    elif execution_mode == "sequential":
        if not eligible:
            raise ConfigError(
                f"execution_mode='sequential' requested but the job is not "
                f"sequential-eligible ({route_reason})."
            )
        if cyclic:
            raise ConfigError(
                "execution_mode='sequential' requested but the FK graph has a "
                "cross-table cycle, which the sequential path cannot order; "
                "use execution_mode='full_frame' or 'auto'."
            )
        if not has_mask_table:
            # NIT (S2 remediation guide section 8): without a mask-kind table
            # the sequential branch below (`has_mask_table and route ==
            # "sequential"`) would silently no-op and fall through to
            # full-frame, ignoring the explicit request. Fail closed instead.
            raise ConfigError(
                "execution_mode='sequential' requested but the job has no "
                "mask-kind table to run through the sequential path."
            )
        route = "sequential"
    else:  # "auto"
        if eligible and cyclic:
            route, route_reason = "full_frame", "cross_table_cycle"
        else:
            route = "sequential" if eligible else "full_frame"

    # S2 early-return: a relationship-bearing pure-mask job routed to
    # sequential skips the full-frame block below entirely (adapter.run +
    # vault + fidelity), the validators block, and the D8 quarantine/
    # fail-loud block -- `run_sequential` (Part 2) is the sole owner of
    # row-error enforcement on this route, so there is no double-processing.
    if has_mask_table and route == "sequential":
        loader = source_loader if source_loader is not None else (lambda t: caller_sources[t])
        seq_result = run_sequential(
            PandasExecutionAdapter(),
            plan,
            loader,
            registry=resolved_registry,
            relationship_graph=graph,
            namespace_registry=ns_registry,
            sink=sink,
            quarantine_config=config.get("quarantine"),
        )
        seq_quality_metrics = dict(seq_result.quality_metrics)
        seq_quality_metrics["execution"] = _execution_telemetry(
            route="sequential",
            route_reason=route_reason,
            sink=sink,
            source_loader=source_loader,
            sources_resident=bool(caller_sources),
        )
        return ExecutionResult(
            outputs=dict(seq_result.outputs),  # {} when a sink was provided
            timings=seq_result.timings,
            boundary_conversion_ms=seq_result.boundary_conversion_ms,
            warnings=seq_result.warnings,
            quality_metrics=seq_quality_metrics,
            table_kinds=table_kinds,
            row_errors=seq_result.row_errors,
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
    if has_mask_table:
        # Merge generate outputs into the sources dict the mask adapter
        # reads. A mask table whose FK parent is a generate table reads the
        # generate output as if it were a source: the generated value IS
        # the FK pool for the mask side.
        merged_sources: dict[str, pa.Table] = {}
        merged_sources.update(caller_sources)
        merged_sources.update(generate_outputs)

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
        # Token vault (deferred follow-up 1): collect source->masked pairs
        # for vault: true columns. Opt-in via the kwarg; the caller writes
        # the artifact.
        if vault_writer is not None:
            from decoy_engine.vault import collect_vault_entries

            vault_writer.add(collect_vault_entries(config, merged_sources, mask_outputs))

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

    # Sprint 2 honesty pack (D7/D8): per-row strategy errors recorded by
    # bucketize/date_shift (format_error, S5) and code_set (mask_error, S6).
    # Collected BEFORE validators run (trap T5: row-error rows are NOT
    # filtered out here -- leak_check and every other validator compare
    # positionally against `sources` and must see the UNFILTERED outputs,
    # or row parity breaks and every subsequent index misattributes).
    mask_row_errors: tuple[Any, ...] = mask_result.row_errors if has_mask_table else ()

    # P2 observe-only planner: classify the job and stamp the decision.
    # Behind the default-off flag so the default path does zero planner
    # work and stays byte-identical; the classification never routes
    # (routing sits behind _planner.PLANNER_ROUTING_ENABLED, hard False).
    if explain_plan:
        from decoy_engine.execution._planner import classify_job

        execution_plan = classify_job(
            config,
            plan=plan,
            registry=resolved_registry,
            relationship_graph=graph,
            substrate=resolved_substrate,
        )
        quality_metrics["execution_plan"] = {
            "mode": execution_plan.mode,
            "reason": execution_plan.reason,
            "rejections": dict(execution_plan.rejections),
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
    quality_metrics["execution"] = _execution_telemetry(
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

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
  7. Call `PandasExecutionAdapter.run(plan, merged_sources, ...)` to
     mask the mask-kind tables. The plan only carries mask-table seeds;
     generate tables are not re-traversed.
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
  mixed-mode adapter for V2 ship.
- Per-node preview on mixed configs. Covered by F5 at the platform
  layer (`run_v2_pipeline_preview`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

if TYPE_CHECKING:
    from decoy_engine.providers_v2 import ProviderRegistry


__all__ = ["classify_table_kinds", "run_pipeline"]


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
    """
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

        adapter = PandasExecutionAdapter()
        mask_result = adapter.run(
            plan,
            merged_sources,
            registry=resolved_registry,
            relationship_graph=graph,
            namespace_registry=ns_registry,
        )
        # The mask adapter returns `outputs` only for mask-kind tables (its
        # work-list iterates over `plan.tables`); generate-kind table
        # entries in `merged_sources` are passed-through input, not output.
        mask_outputs = dict(mask_result.outputs)
        mask_timings = mask_result.timings
        mask_conversion_ms = mask_result.boundary_conversion_ms
        mask_warnings = mask_result.warnings
        mask_quality_metrics = mask_result.quality_metrics
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

        if validation_covered or row_errors_covered:
            from decoy_engine.quarantine import apply_quarantine, quarantine_manifest

            outputs, q_summary = apply_quarantine(
                outputs, v_report, quarantine_cfg, row_errors=mask_row_errors
            )
            quality_metrics["quarantine"] = quarantine_manifest(q_summary)

        if validator_failed and not validation_covered:
            raise ValidatorFailedError(v_report)
        if row_errors_uncovered:
            raise RowErrorsFailedError(row_errors_uncovered)

    return ExecutionResult(
        outputs=outputs,
        timings=mask_timings,
        boundary_conversion_ms=mask_conversion_ms,
        warnings=mask_warnings,
        quality_metrics=quality_metrics,
        table_kinds=table_kinds,
        row_errors=mask_row_errors,
    )

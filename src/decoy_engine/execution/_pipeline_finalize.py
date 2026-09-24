"""Post-mask finalize helpers for `run_pipeline`, split out to hold the
600-LOC orchestration cap (CLAUDE.md "Engineering best practices").

These are POST-mask, full-frame-only concerns -- everything that happens
to `outputs` / `quality_metrics` AFTER the mask/generate step has already
produced its tables, on the branch that did NOT take one of the routed
early returns (`_pipeline_routing.run_sequential_route` /
`run_out_of_core_route`, which own their own telemetry and never reach
here). Kept in a separate module from `_pipeline_routing` because these
are finalize/stamping concerns, not routing decisions: nothing here picks
a route, it only records what already happened or enforces a policy over
already-produced outputs.

Three independent pieces:

- `stamp_execution_metrics`: the two reproducibility stamps (selected
  adapter identity + auto-chunk decision) that make a non-default
  performance-mode run reproducible from its manifest.
- `compute_fidelity_reports`: BF1 opt-in, report-only per-mask-table
  distribution fidelity (never fails the job).
- `finalize_validators_and_quarantine`: D8's combined validator +
  row-error quarantine pass with its fail-closed remainder rule.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._planner import AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile import Profile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph

__all__ = [
    "compute_fidelity_reports",
    "compute_post_validation",
    "finalize_validators_and_quarantine",
    "stamp_execution_metrics",
]

# `run_pipeline`'s execution-knob defaults. Owned here (not `_pipeline.py`,
# which imports them back for its own signature defaults) so the single
# source of truth lives next to the one place that reads it to decide
# non-default-ness -- the original split (defaults in `_pipeline.py`,
# non-default booleans computed there too) needed no cross-module import,
# but computing the booleans here instead does, and importing a bare literal
# back across that boundary would create exactly the drift risk the old
# comment warned against. `substrate` pins "pandas" (NOT None): pinning it
# keeps run_pipeline's default route byte-identical to the original hardcoded
# pandas path regardless of any DECOY_SUBSTRATE the environment sets.
SUBSTRATE_DEFAULT = "pandas"
FPE_CHUNK_COUNT_DEFAULT = 4
MAX_WORKERS_DEFAULT = 4
FALLBACK_TO_PANDAS_DEFAULT = True
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
AUTO_CHUNK_DEFAULT = True
CHUNK_SIZE_ROWS_DEFAULT = 50_000
AUTO_CHUNK_THRESHOLD_DEFAULT = AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT


def stamp_execution_metrics(
    mask_quality_metrics: dict[str, Any],
    *,
    adapter: Any,
    substrate: str | None,
    resolved_substrate: str,
    fpe_chunk_count: int,
    max_workers: int,
    fallback_to_pandas: bool,
    route_chunked: bool,
    auto_chunk: bool,
    chunk_size_rows: int,
    auto_chunk_threshold_rows: int,
    table_kinds: dict[str, str],
    caller_sources: dict[str, pa.Table],
    execution_plan_decision: Any,
) -> None:
    """Mutate `mask_quality_metrics` in place with the two performance-mode
    reproducibility stamps.

    Performance-mode reproducibility: any non-default knob stamps the
    selected adapter identity + every knob value into the job metadata.
    The all-default path stamps nothing so golden and compat-corpus
    fixtures stay byte-identical. `adapter_non_default` / `auto_chunk_
    non_default` are computed here (against this module's own default
    constants above) rather than by the caller, so `run_pipeline` threads
    only the raw knob values through, not a pre-computed comparison.

    Auto-chunk reproducibility stamp: a routed run is non-default by
    definition; a non-routed run stamps only when an auto-chunk knob is
    non-default. All-default full-frame runs stamp nothing (the P1
    golden `quality_metrics == {}` contract).
    """
    adapter_non_default = (substrate, fpe_chunk_count, max_workers, fallback_to_pandas) != (
        SUBSTRATE_DEFAULT,
        FPE_CHUNK_COUNT_DEFAULT,
        MAX_WORKERS_DEFAULT,
        FALLBACK_TO_PANDAS_DEFAULT,
    )
    auto_chunk_non_default = (auto_chunk, chunk_size_rows, auto_chunk_threshold_rows) != (
        AUTO_CHUNK_DEFAULT,
        CHUNK_SIZE_ROWS_DEFAULT,
        AUTO_CHUNK_THRESHOLD_DEFAULT,
    )
    if adapter_non_default:
        mask_quality_metrics["execution_adapter"] = {
            "adapter_name": adapter.adapter_name,
            "adapter_version": adapter.adapter_version,
            "requested_substrate": substrate,
            "resolved_substrate": resolved_substrate,
            "fpe_chunk_count": fpe_chunk_count,
            "max_workers": max_workers,
            "fallback_to_pandas": fallback_to_pandas,
        }

    if route_chunked or auto_chunk_non_default:
        from decoy_engine.execution import _pipeline_routing

        mask_quality_metrics["auto_chunk"] = _pipeline_routing.auto_chunk_stamp(
            route_chunked=route_chunked,
            auto_chunk=auto_chunk,
            chunk_size_rows=chunk_size_rows,
            auto_chunk_threshold_rows=auto_chunk_threshold_rows,
            table_kinds=table_kinds,
            caller_sources=caller_sources,
            decision=execution_plan_decision,
        )


def compute_fidelity_reports(
    mask_outputs: dict[str, pa.Table],
    merged_sources: dict[str, pa.Table],
    *,
    table_kinds: dict[str, str],
    now_iso: str | None,
) -> dict[str, Any]:
    """BF1: opt-in, report-only distribution fidelity per mask table.

    Imported lazily (via the `compute_quality_report` import below) so the
    default-OFF path never pulls in the quality stack. SECURITY: emit ONLY
    the assembled report (aggregate, label-free); the intermediate
    snapshots that carry raw category values stay inside
    `compute_quality_report` and are never attached. First slice is
    mask-kind tables, marginal-only (no `joint_columns`); generate-kind
    tables are skipped. Called only when `fidelity_report=True`, so the
    caller pays this cost opt-in.
    """
    from decoy_engine.quality.report import compute_quality_report

    fidelity_reports: dict[str, Any] = {}
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
    return fidelity_reports


def _align_sources_to_output(
    sources: Mapping[str, pa.Table],
    quarantine_row_mask: Mapping[str, set[int]],
) -> Mapping[str, pa.Table]:
    """Drop the quarantined rows from `sources` so it lines up with the output.

    Quarantine filters rows out of the output but leaves `sources` (the pre-mask
    input) whole, so a row-count-dependent scan (null_audit) would read the gap as
    a mismatch and hard-fail on a job where quarantine merely did its job. The
    post-mask output and its source are 1:1 by row on the full-frame path, so the
    same per-table row indices quarantine removed from the output apply to the
    source. Returns `sources` unchanged when nothing was quarantined.
    """
    if not quarantine_row_mask:
        return sources
    aligned: dict[str, pa.Table] = {}
    for name, table in sources.items():
        bad = quarantine_row_mask.get(name)
        if not bad:
            aligned[name] = table
            continue
        keep = pa.array([i not in bad for i in range(table.num_rows)], type=pa.bool_())
        aligned[name] = table.filter(keep)
    return aligned


def _filter_sampled_values_source_equality(
    quality_summary: dict[str, Any],
    sources: Mapping[str, pa.Table],
) -> None:
    """Strip any sampled value equal to a source value in the same column (R18).

    `sampled_values` is read from the masked OUTPUT of non-passthrough columns, on
    the assumption the output is synthetic. That holds for substitution strategies
    but not universally: a value-reuse strategy (shuffle, categorical) re-emits
    source values by design, and a leaking substitution echoes one by accident, so
    an output-only read can carry source PII into the manifest. This per-column
    source-equality filter removes any sampled value that also appears in that
    column's source. A column whose sampled values are all source-equal (e.g. a
    shuffle column) drops its entry entirely rather than surfacing an empty list:
    it has no synthetic evidence to show. Filters against the full pre-quarantine
    source (not the row-aligned one) so a value that moved out of a quarantined
    row's position still cannot slip through.
    """
    sampled: dict[str, list[Any]] = quality_summary.get("sampled_values") or {}
    if not sampled:
        return
    # Build the key -> source-value-set map from the REAL (table, column) pairs in
    # `sources`, never by splitting the "table.column" key. But `f"{table}.{col}"`
    # is not injective: table "a.b"/column "c" and table "a"/column "b.c" both
    # flatten to "a.b.c", so a plain assignment would let whichever pair iterates
    # LAST overwrite the other, and a sample could then be filtered against the
    # WRONG column and a source value survive. Accumulate the UNION of every
    # colliding column's source values instead, so a sampled value is stripped
    # when it equals a source value in ANY pair that maps to its key -- the result
    # no longer depends on `sources` insertion order, and no source value slips
    # through a dotted-name collision.
    needed = set(sampled)
    source_by_key: dict[str, set[Any]] = {}
    for table_name, table in sources.items():
        for col in table.column_names:
            key = f"{table_name}.{col}"
            if key in needed:
                source_by_key.setdefault(key, set()).update(
                    v for v in table.column(col).to_pylist() if v is not None
                )
    filtered: dict[str, list[Any]] = {}
    for key, values in sampled.items():
        source_values = source_by_key.get(key)
        if source_values is None:
            filtered[key] = values  # no resolvable source column: leave untouched
            continue
        kept = [v for v in values if v not in source_values]
        if kept:
            filtered[key] = kept
    quality_summary["sampled_values"] = filtered


def compute_post_validation(
    execution_result: ExecutionResult,
    *,
    plan: Plan,
    sources: Mapping[str, pa.Table],
    quarantine_row_mask: Mapping[str, set[int]],
    profile: Profile,
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
    namespace_registry: NamespaceRegistry,
    post_validation: bool,
    post_validation_skip: list[str],
    post_validation_sample_size: int,
    post_validation_enforce: bool,
) -> None:
    """A1: opt-in, default-OFF post-execution scan suite wired into the run path.

    Sibling of `compute_fidelity_reports`: another report-shaped attachment to
    `ExecutionResult.quality_metrics` that runs only when its runtime flag is on.
    Flag off (the default) -> this returns before importing `validation.post`, so
    the hot path is unchanged and the output byte-identical. Flag on -> it builds
    the `config` dict the `PostValidationRunner` reads and calls it; the runner
    scans the masked output against the sources and writes the full manifest block
    under `quality_metrics["quality_summary"]` (mutating the mutable dict the
    frozen `ExecutionResult` holds).

    Job-outcome signal (LOCKED: warn-only default). The runner sets
    `QualitySummary.failed_checks` (the hard-failed scan names). This surfaces them
    as `quality_metrics["failed_checks"]`, a list of `{"code", "message"}` dicts --
    the exact shape the platform node-run consumer already reads
    (decoy-platform `api/jobs/v2_node_runs.py` `_classify_quality_failures`). By
    default those codes are evidence-only there (a finding is recorded, the job
    still succeeds). `post_validation_enforce` is emitted as
    `quality_metrics["post_validation_enforce"]` for the platform's companion
    change to branch on: enforce true + a non-empty `failed_checks` promotes the
    hard-fails to a job failure. The engine never fails the job itself.

    Privacy (R18): `sampled_values` is read from the masked output, but a
    value-reuse strategy or a leaking substitution can echo a source value into
    it, so this wiring runs a per-column source-equality filter over the runner's
    output before it reaches `quality_metrics` (see
    `_filter_sampled_values_source_equality`); no source PII survives into the
    manifest.

    Quarantine alignment: when quarantine removed rows, the output has fewer rows
    than `sources`. Positional / row-count scans (null_audit) run against the
    row-aligned source (see `_align_sources_to_output`) so a successful quarantine
    does not read as a row-count mismatch. Value-membership scans (leakage) instead
    scan the FULL pre-quarantine source: a quarantine that drops a source row whose
    value still appears in a retained masked row must not hide that substitution
    leak. The runner selects per scan-class (see `PostValidationRunner.run` +
    `validation.post._checks.FULL_SOURCE_SCANS`); the privacy filter also uses the
    full source.
    """
    if not post_validation:
        return
    from decoy_engine.validation.post import PostValidationRunner

    aligned_sources = _align_sources_to_output(sources, quarantine_row_mask)
    summary = PostValidationRunner().run(
        plan=plan,
        execution_result=execution_result,
        sources=aligned_sources,
        full_sources=sources,
        profile=profile,
        registry=registry,
        relationship_graph=relationship_graph,
        namespace_registry=namespace_registry,
        config={
            "post_validation": True,
            "post_validation_skip": list(post_validation_skip),
            "post_validation_sample_size": post_validation_sample_size,
        },
    )
    if summary is None:  # pragma: no cover - post_validation=True always returns a summary
        return
    qm = execution_result.quality_metrics
    quality_summary = qm.get("quality_summary")
    if quality_summary is not None:
        _filter_sampled_values_source_equality(quality_summary, sources)
    qm["failed_checks"] = [
        {"code": name, "message": f"post-validation scan {name!r} hard-failed"}
        for name in summary.failed_checks
    ]
    qm["post_validation_enforce"] = post_validation_enforce


def finalize_validators_and_quarantine(
    outputs: dict[str, pa.Table],
    *,
    config: dict[str, Any],
    caller_sources: dict[str, pa.Table],
    mask_row_errors: tuple[Any, ...],
    quality_metrics: dict[str, Any],
) -> tuple[dict[str, pa.Table], dict[str, set[int]]]:
    """SP-05 job-level validators (P5.INFRA.4) + D8 combined quarantine pass.

    Validators run AFTER all column passes complete, on the UNFILTERED
    outputs (trap T5). Fail-closed by default: a validator failure raises
    `ValidatorFailedError` unless quarantine is enabled with the
    `validation_fail` trigger.

    D8: one combined quarantine pass over validator findings + row errors,
    then a fail-closed remainder rule for anything quarantine did not
    cover. Quarantine JSONL is durable only on a successful (fully
    covered) run; a fail-loud run publishes nothing.

    Mutates `quality_metrics` in place (validation / row_errors /
    quarantine keys) and returns the (possibly quarantine-filtered)
    `outputs` dict plus the per-table set of row indices quarantine removed
    (empty when nothing was quarantined). The caller reassigns `outputs` from
    the first return value; post-validation uses the second to align the
    sources it scans to the filtered output.
    """
    quarantine_removed: dict[str, set[int]] = {}
    validators_config: list[Any] = config.get("validators") or []
    v_report: Any = None
    if validators_config:
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
            from decoy_engine.quarantine import (
                apply_quarantine,
                quarantine_manifest,
                quarantine_row_mask,
            )

            # Capture the removed row set against the pre-filter outputs before
            # apply_quarantine rebinds `outputs` to the filtered tables.
            quarantine_removed = quarantine_row_mask(
                outputs, v_report, quarantine_cfg, row_errors=mask_row_errors
            )
            outputs, q_summary = apply_quarantine(
                outputs, v_report, quarantine_cfg, row_errors=mask_row_errors
            )
            quality_metrics["quarantine"] = quarantine_manifest(q_summary)

    return outputs, quarantine_removed

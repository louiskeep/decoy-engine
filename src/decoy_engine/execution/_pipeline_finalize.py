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
from typing import Any

import pyarrow as pa

__all__ = [
    "compute_fidelity_reports",
    "finalize_validators_and_quarantine",
    "stamp_execution_metrics",
]


def stamp_execution_metrics(
    mask_quality_metrics: dict[str, Any],
    *,
    adapter: Any,
    substrate: str | None,
    resolved_substrate: str,
    fpe_chunk_count: int,
    max_workers: int,
    fallback_to_pandas: bool,
    adapter_non_default: bool,
    route_chunked: bool,
    auto_chunk: bool,
    chunk_size_rows: int,
    auto_chunk_threshold_rows: int,
    auto_chunk_non_default: bool,
    table_kinds: dict[str, str],
    caller_sources: dict[str, pa.Table],
    execution_plan_decision: Any,
) -> None:
    """Mutate `mask_quality_metrics` in place with the two performance-mode
    reproducibility stamps.

    Performance-mode reproducibility: any non-default knob stamps the
    selected adapter identity + every knob value into the job metadata.
    The all-default path stamps nothing so golden and compat-corpus
    fixtures stay byte-identical. `adapter_non_default` /
    `auto_chunk_non_default` are pre-computed by the caller (a plain
    tuple-equality check against `run_pipeline`'s own default constants)
    so this module does not need to import or duplicate them.

    Auto-chunk reproducibility stamp: a routed run is non-default by
    definition; a non-routed run stamps only when an auto-chunk knob is
    non-default. All-default full-frame runs stamp nothing (the P1
    golden `quality_metrics == {}` contract).
    """
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


def finalize_validators_and_quarantine(
    outputs: dict[str, pa.Table],
    *,
    config: dict[str, Any],
    caller_sources: dict[str, pa.Table],
    mask_row_errors: tuple[Any, ...],
    quality_metrics: dict[str, Any],
) -> dict[str, pa.Table]:
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
    `outputs` dict; the caller's `outputs` binding must be reassigned from
    the return value.
    """
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
            from decoy_engine.quarantine import apply_quarantine, quarantine_manifest

            outputs, q_summary = apply_quarantine(
                outputs, v_report, quarantine_cfg, row_errors=mask_row_errors
            )
            quality_metrics["quarantine"] = quarantine_manifest(q_summary)

    return outputs

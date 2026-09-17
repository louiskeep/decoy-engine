"""The generate + mask execution steps of `run_pipeline`, split out to hold
the 645-LOC orchestration cap (CLAUDE.md "Engineering best practices",
`tests/sentry/test_module_size.py` ALLOWLIST).

This is Steps 1 and 2 of the module docstring's nine-step sequencing
contract on `_pipeline.py`: run generate-kind tables (Plan-only), merge
their outputs into the mask adapter's sources, then run the mask-kind
tables through the selected route (chunked or full-frame) and stamp the
BF1 fidelity report + reproducibility metrics. `run_pipeline` calls this
once, on the branch that did NOT take one of the routed early returns
(sequential / out_of_core / native / unified-slice, which own their own
generate+mask dispatch and never reach here). Pure code move out of that
module; no behavior change.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution import _pipeline_finalize
from decoy_engine.execution import _pipeline_route_exec as _route_exec

__all__ = ["GenerateMaskStepResult", "run_generate_and_mask_steps"]

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from faker import Faker

    from decoy_engine.execution._adapter import ExecutionAdapter
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


@dataclasses.dataclass(frozen=True)
class GenerateMaskStepResult:
    """Everything Step 3 (stitch) and the finalize stage of `run_pipeline`
    need from the generate + mask steps. Field names mirror the locals
    `run_pipeline` bound before this split so the call site is a plain
    unpack, not a remap."""

    generate_outputs: dict[str, pa.Table]
    mask_outputs: dict[str, pa.Table]
    mask_timings: tuple[Any, ...]
    mask_conversion_ms: float
    mask_warnings: tuple[Any, ...]
    mask_quality_metrics: dict[str, Any]
    mask_row_errors: tuple[Any, ...]
    fidelity_reports: dict[str, Any]


def run_generate_and_mask_steps(
    *,
    has_generate_table: bool,
    has_mask_table: bool,
    plan: Plan,
    derive_key: Any,
    instance_default_locale: str | None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
    resident_sources: dict[str, pa.Table],
    route_chunked: bool,
    table_kinds: dict[str, str],
    config: dict[str, Any],
    engine_version: str,
    registry: ProviderRegistry,
    adapter: ExecutionAdapter,
    vault_writer: Any,
    chunk_size_rows: int,
    key_provider: KeyProvider | None,
    graph: RelationshipGraph,
    namespace_registry: NamespaceRegistry,
    unconfigured_column_policy: UnconfiguredColumnPolicy,
    generate_output_tables: frozenset[str],
    substrate: str | None,
    resolved_substrate: str,
    fpe_chunk_count: int,
    max_workers: int,
    fallback_to_pandas: bool,
    auto_chunk: bool,
    auto_chunk_threshold_rows: int,
    execution_plan_decision: ExecutionPlan | None,
    fidelity_report: bool,
    now_iso: str | None,
) -> GenerateMaskStepResult:
    """Run generate-kind tables then mask-kind tables (`_pipeline.py`
    Steps 1-2), returning everything the stitch + finalize stages need.
    See the module docstring for why this is a separate function."""
    from decoy_engine.generation.synthesize import generate_tables

    # Step 1: generate-kind tables. Plan-only (guide 4.8/9): passing the
    # whole Plan is safe even with mask tables present, since synthesize
    # filters by `generate_columns` presence internally.
    generate_outputs: dict[str, pa.Table] = {}
    if has_generate_table:
        generate_outputs = generate_tables(
            plan,
            derive_key=derive_key,
            instance_default_locale=instance_default_locale,
            provider_snapshot=provider_snapshot,
        )

    # Step 2: mask-kind tables.
    mask_outputs: dict[str, pa.Table] = {}
    mask_timings: tuple[Any, ...] = ()
    mask_conversion_ms: float = 0.0
    mask_warnings: tuple[Any, ...] = ()
    mask_quality_metrics: dict[str, Any] = {}
    fidelity_reports: dict[str, Any] = {}
    # Honesty pack (D7/D8): populated from `mask_result.row_errors` on the
    # full-frame branch below. The chunked branch leaves this `()` by
    # construction -- see `_pipeline_route_exec.run_mask_chunked`'s docstring:
    # a routed job that reaches this point is never eligible for row-error
    # quarantine (same policy the manual chunked entrypoint enforces).
    mask_row_errors: tuple[Any, ...] = ()
    if has_mask_table:
        # Merge generate outputs into the sources dict the mask adapter
        # reads. A mask table whose FK parent is a generate table reads the
        # generate output as if it were a source: the generated value IS
        # the FK pool for the mask side.
        merged_sources: dict[str, pa.Table] = {}
        merged_sources.update(resident_sources)
        merged_sources.update(generate_outputs)

        if route_chunked:
            # The eligible shape is exactly one mask table with no generate
            # tables, so merged_sources holds only that table's frame; the
            # planner's runtime gates already rejected anything else.
            mask_table_name = next(name for name, kind in table_kinds.items() if kind == "mask")
            mask_outputs, mask_timings, mask_conversion_ms, mask_warnings, mask_quality_metrics = (
                _route_exec.run_mask_chunked(
                    config,
                    merged_sources[mask_table_name],
                    table=mask_table_name,
                    engine_version=engine_version,
                    registry=registry,
                    adapter=adapter,
                    vault_writer=vault_writer,
                    chunk_size_rows=chunk_size_rows,
                    key_provider=key_provider,
                )
            )
        else:
            mask_result = adapter.run(
                plan,
                merged_sources,
                registry=registry,
                relationship_graph=graph,
                namespace_registry=namespace_registry,
                unconfigured_column_policy=unconfigured_column_policy,
                generate_output_tables=generate_output_tables,
                key_provider=key_provider,
            )
            # Adapters echo every source frame in `outputs` (generate-kind
            # entries in `merged_sources` come back round-tripped through the
            # substrate). Keeping them all preserves the established stitch
            # contract below, where mask_result wins ties over the raw generate outputs.
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
        # Reproducibility stamps (selected adapter identity + auto-chunk
        # decision) and the BF1 fidelity report are finalize-only concerns;
        # see `_pipeline_finalize` for the full "why" on each, including how
        # it derives non-default-ness from the raw knobs below.
        _pipeline_finalize.stamp_execution_metrics(
            mask_quality_metrics,
            adapter=adapter,
            substrate=substrate,
            resolved_substrate=resolved_substrate,
            fpe_chunk_count=fpe_chunk_count,
            max_workers=max_workers,
            fallback_to_pandas=fallback_to_pandas,
            route_chunked=route_chunked,
            auto_chunk=auto_chunk,
            chunk_size_rows=chunk_size_rows,
            auto_chunk_threshold_rows=auto_chunk_threshold_rows,
            table_kinds=table_kinds,
            caller_sources=resident_sources,
            execution_plan_decision=execution_plan_decision,
        )

        if fidelity_report:
            fidelity_reports = _pipeline_finalize.compute_fidelity_reports(
                mask_outputs,
                merged_sources,
                table_kinds=table_kinds,
                now_iso=now_iso,
            )

    return GenerateMaskStepResult(
        generate_outputs=generate_outputs,
        mask_outputs=mask_outputs,
        mask_timings=mask_timings,
        mask_conversion_ms=mask_conversion_ms,
        mask_warnings=mask_warnings,
        mask_quality_metrics=mask_quality_metrics,
        mask_row_errors=mask_row_errors,
        fidelity_reports=fidelity_reports,
    )

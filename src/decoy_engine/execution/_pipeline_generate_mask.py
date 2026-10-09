"""The generate + mask execution steps of `run_pipeline`, split out to hold
the 645-LOC orchestration cap (CLAUDE.md "Engineering best practices",
`tests/sentry/test_module_size.py` ALLOWLIST).

This is Steps 1 and 2 of the module docstring's nine-step sequencing
contract on `_pipeline.py`: run generate-kind tables (Plan-only), merge
their outputs into the mask adapter's sources, then run the mask-kind
tables through the selected route (chunked, per-table split or full-frame) and stamp the
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

from decoy_engine.execution import (
    _chunked_input,
    _pipeline_auto_chunk,
    _pipeline_finalize,
    _pipeline_sources,
)
from decoy_engine.execution import _chunked_output_sink as _output_sink
from decoy_engine.execution import _pipeline_multi_table as _multi_table
from decoy_engine.execution import _pipeline_route_exec as _route_exec
from decoy_engine.profile._readers import LazySource

__all__ = ["GenerateMaskStepResult", "run_generate_and_mask_steps"]

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from faker import Faker

    from decoy_engine.execution._adapter import ExecutionAdapter
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.generation.pool import PoolCache
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
    # The caller's sources as the finalize stage reads them: every table resident, except on
    # a run that streamed its lazy inputs, where `LazySource` values stay and no consumer of
    # the whole sources exists (`decide_output_mode`).
    sources: dict[str, pa.Table]
    inputs_streamed: bool


def run_generate_and_mask_steps(
    *,
    has_generate_table: bool,
    has_mask_table: bool,
    plan: Plan,
    derive_key: Any,
    instance_default_locale: str | None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
    resident_sources: dict[str, Any],
    caller_sources: Mapping[str, Any],
    keep_lazy: Mapping[str, _chunked_input.SourceFacts],
    route_chunked: bool,
    table_kinds: dict[str, str],
    config: dict[str, Any],
    engine_version: str,
    registry: ProviderRegistry,
    adapter: ExecutionAdapter,
    vault_writer: Any,
    chunk_size_rows: int,
    native_threads: int,
    pool_cache: PoolCache,
    chunked_dispatcher_enabled: bool,
    multi_table_dispatch_enabled: bool,
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
    publish: _output_sink.OutputPublish,
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
    sources = dict(resident_sources)
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

        # B7: an independent multi-table job runs each table on the route it would take
        # alone. `None` leaves the branches below exactly as they were.
        split = (
            None
            if route_chunked
            else _multi_table.decide_multi_table_split(
                config,
                plan=plan,
                registry=registry,
                graph=graph,
                substrate=resolved_substrate,
                caller_sources=caller_sources,
                table_kinds=table_kinds,
                auto_chunk=auto_chunk,
                source_facts=keep_lazy,
                auto_chunk_threshold_rows=auto_chunk_threshold_rows,
                dispatcher_enabled=chunked_dispatcher_enabled,
                split_enabled=multi_table_dispatch_enabled,
                vault_writer_present=vault_writer is not None,
            )
        )
        # B6a: decided once, before the first chunk; a streaming run opens the publish session.
        out_mode, out_reason = ("resident", "")
        if split is not None or route_chunked:
            out_mode, out_reason = _output_sink.decide_output_mode(
                stream_chunked_output=publish.stream,
                sink=publish.sink,
                dispatcher_enabled=chunked_dispatcher_enabled,
                config=config,
                fidelity_report=fidelity_report,
                post_validation=publish.post_validation,
                split=split,
                resident_names=tuple(merged_sources),
            )
            if out_mode == "streamed":
                publish.open()
        # B6b: a lazy table streams only when the output does and a lane takes it; every other
        # one is materialized exactly once, here, before any lane runs.
        modes = _chunked_input.input_modes(
            keep_lazy,
            out_mode=out_mode,
            out_reason=out_reason,
            routed=next(n for n, k in table_kinds.items() if k == "mask")
            if route_chunked
            else None,
            split=split,
        )
        for name, (mode, _why) in modes.items():
            if mode == "resident":
                sources[name] = merged_sources[name] = _pipeline_sources.materialize_source(
                    sources[name]
                )
        if split is not None:
            (
                mask_outputs,
                mask_timings,
                mask_conversion_ms,
                mask_warnings,
                mask_quality_metrics,
                mask_row_errors,
            ) = _multi_table.run_multi_table_split(
                split,
                config,
                resident_sources=merged_sources,
                input_facts=keep_lazy,
                input_reasons={n: why for n, (mode, why) in modes.items() if mode == "resident"},
                engine_version=engine_version,
                registry=registry,
                adapter=adapter,
                chunk_size_rows=chunk_size_rows,
                key_provider=key_provider,
                native_threads=native_threads,
                plan=plan,
                graph=graph,
                namespace_registry=namespace_registry,
                unconfigured_column_policy=unconfigured_column_policy,
                generate_output_tables=generate_output_tables,
                sink=publish.active_sink,
                output_reason=out_reason if out_mode == "resident" else None,
            )
        elif route_chunked:
            # The eligible shape is exactly one mask table with no generate
            # tables, so merged_sources holds only that table's frame; the
            # planner's runtime gates already rejected anything else.
            mask_table_name = next(name for name, kind in table_kinds.items() if kind == "mask")
            # A streaming run calls the lane directly with the sink; the resident delegate
            # `_route_exec.run_mask_chunked` stays as it was.
            lane = (
                _pipeline_auto_chunk.run_auto_chunk
                if out_mode == "streamed"
                else _route_exec.run_mask_chunked
            )
            mask_outputs, mask_timings, mask_conversion_ms, mask_warnings, mask_quality_metrics = (
                lane(
                    config,
                    merged_sources[mask_table_name],
                    table=mask_table_name,
                    engine_version=engine_version,
                    registry=registry,
                    adapter=adapter,
                    vault_writer=vault_writer,
                    chunk_size_rows=chunk_size_rows,
                    key_provider=key_provider,
                    native_threads=native_threads,
                    dispatcher_enabled=chunked_dispatcher_enabled,
                    **(
                        {"sink": publish.active_sink, "expected": keep_lazy.get(mask_table_name)}
                        if out_mode == "streamed"
                        else {}
                    ),
                )
            )
            if out_mode == "resident":
                lane_block = mask_quality_metrics.setdefault("auto_chunk", {})
                lane_block["output"] = _output_sink.resident_block(out_reason)
                lane_block["input"] = _chunked_input.resident_block(
                    modes.get(mask_table_name, ("", _chunked_input.REASON_RESIDENT_SOURCE))[1]
                )
        else:
            mask_result = adapter.run(
                plan,
                merged_sources,
                registry=registry,
                pool_cache=pool_cache,
                relationship_graph=graph,
                namespace_registry=namespace_registry,
                unconfigured_column_policy=unconfigured_column_policy,
                generate_output_tables=generate_output_tables,
                key_provider=key_provider,
                # Full-frame route: pin an admitted deterministic-Faker column's degenerate
                # output to `string` (C5c-ii option A). The chunked legs pin their own way.
                pin_degenerate_faker=True,
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
            # per chunk inside its lane (`_pipeline_auto_chunk`) instead.
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
            caller_sources=sources,
            execution_plan_decision=execution_plan_decision,
            multi_table_split=split,
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
        sources=sources,
        inputs_streamed=bool(sources) and all(isinstance(v, LazySource) for v in sources.values()),
    )

"""The production mixed (generate + mask) route executor.

Generation stays resident and unchanged: `generate_tables` runs exactly as the
full-frame oracle runs it. The mask half runs on the bounded chunked lane
(`_pipeline_auto_chunk.run_auto_chunk`, the compiled kernels when the companion
is present), one lane run per mask table, and the result is byte-identical to the
full-frame mixed oracle on the admitted shapes.

Admission is two-stage. The STATIC stage (`_mixed_static`) decided before
anything was generated that this route may run. The RUNTIME stage here looks at
the materialized generate output (`_mixed_admission`: every generate column must
survive the oracle's pandas echo, the coupled parent key must be int/string) and
at the lazy sources' footers, all before the first sink write. A runtime decline
hands the already-generated outputs to the full-frame continuation
(`run_full_frame_route(pregenerated=...)`), so nothing is regenerated.

Coupled jobs (exactly one generate-parent -> mask-child edge) resolve the
child's FK key over the whole table against the resident parent in one call
(`_mixed_fk_bridge`), then mask the child's payload columns chunked while the
resolved key rides the stream as a pre-resolved column
(`_chunked_preresolved`). One resolve means one edge-level `orphan_fk` warning
whose count is the whole-column count, the oracle's shape, with no cross-chunk
summing. FAIL and REMAP-orphan raise the oracle's own errors.

Publishing reuses `OutputPublish` over the caller's `TransactionalSink`: one
session, every generate table and every masked table written in FK-topological
order, exactly one commit, abort on any failure (including a failure while the
key is still being resolved). Without a streaming sink the outputs are returned
in the result, as the full-frame route returns them.

Established patterns: the generate->mask hand-off is the oracle's own
(`_pipeline_generate_mask`); the chunked lane and its schema rule are B1/B2's;
the staged-then-renamed publish is Spark's `FileOutputCommitter` protocol as
`_chunked_output_sink` documents.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import reasons
from decoy_engine.declared_casting._boundary import open_cast_run
from decoy_engine.declared_casting._eligibility import declared_route_guard
from decoy_engine.execution import (
    _chunked_input,
    _pipeline_auto_chunk,
    _pipeline_finalize,
    _pipeline_sources,
)
from decoy_engine.execution import _chunked_output_sink as _output_sink
from decoy_engine.execution import _mixed_admission as admission
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._chunked_preresolved import PreResolvedColumn
from decoy_engine.execution._mixed_fk_bridge import resolve_generate_parent_fk_child
from decoy_engine.execution._mixed_static import MixedStaticPlan, write_order
from decoy_engine.execution._stitch import stitch_generate_mask_outputs
from decoy_engine.execution._transforms_admission import stamp_out_of_core_declined
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.execution._pipeline_context import PipelineRunContext, RouteDecision

__all__ = ["run_mixed_route"]

ROUTE_NAME = "mixed"


def _generate(ctx: PipelineRunContext) -> dict[str, pa.Table]:
    from decoy_engine.generation.synthesize import generate_tables

    return generate_tables(
        ctx.plan,
        derive_key=ctx.derive_key,
        instance_default_locale=ctx.instance_default_locale,
        provider_snapshot=ctx.provider_snapshot,
    )


def _runtime_decline(
    static: MixedStaticPlan, ctx: PipelineRunContext, generate_outputs: dict[str, pa.Table]
) -> str | None:
    """Why the route cannot publish THIS run's materialized inputs, else `None`.
    Nothing has been written when this runs."""
    try:
        admission.require_roundtrip_stable_generate_outputs(generate_outputs)
        if static.edge is not None:
            admission.require_admitted_parent_key_type(static.edge, generate_outputs)
    except admission.MixedDeclineError as decline:
        return decline.code
    for name, expected in static.source_facts.items():
        source = ctx.caller_sources.get(name)
        if not isinstance(source, LazySource) or not _chunked_input.facts_match(
            _chunked_input.source_facts(source), expected
        ):
            return reasons.runtime.LAZY_SOURCE_CHANGED
    return None


def _fall_back(
    ctx: PipelineRunContext,
    decision: RouteDecision,
    generate_outputs: dict[str, pa.Table],
    reason: str,
) -> ExecutionResult:
    """Continue on the full-frame path, reusing the generate outputs already built."""
    from decoy_engine.execution import _pipeline_full_frame

    declined = dataclasses.replace(
        decision,
        route="full_frame",
        route_reason=f"{reasons.route.MIXED_RUNTIME_DECLINED}:{reason}",
        mixed=None,
    )
    return _pipeline_full_frame.run_full_frame_route(ctx, declined, pregenerated=generate_outputs)


def _read_key_column(source: pa.Table | LazySource, column: str) -> pa.Array:
    """The child key column alone, from the same prepared source the payload stream
    reads (a lazy source projects one column from its file)."""
    if isinstance(source, LazySource):
        table = pq.read_table(source.path, columns=[column])
    else:
        table = source
    return table.column(column).combine_chunks()


def _build_carrier(
    static: MixedStaticPlan,
    sources: dict[str, pa.Table | LazySource],
    generate_outputs: dict[str, pa.Table],
) -> tuple[PreResolvedColumn | None, tuple[Any, ...]]:
    edge = static.edge
    if edge is None:
        return None, ()
    column = edge.child_columns[0]
    source_key = _read_key_column(sources[edge.child_table], column)
    resolution = resolve_generate_parent_fk_child(
        edge,
        parent_table=generate_outputs[edge.parent_table],
        child_table=pa.table({column: source_key}),
    )
    carrier = PreResolvedColumn(edge.child_table, column, source_key, resolution.column)
    return carrier, resolution.warnings


def run_mixed_route(ctx: PipelineRunContext, decision: RouteDecision) -> ExecutionResult:
    declared_route_guard("mixed", plan=ctx.plan, raise_on_decline=True)
    static = decision.mixed
    if static is None:  # pragma: no cover - the router sets both together
        raise AssertionError("run_mixed_route reached without a static admission")

    generate_outputs = _generate(ctx)
    declined = _runtime_decline(static, ctx, generate_outputs)
    if declined is not None:
        return _fall_back(ctx, decision, generate_outputs, declined)
    cast_run = open_cast_run(ctx.plan, ctx.quarantine_runtime)
    try:
        return _run_admitted(ctx, decision, static, generate_outputs, cast_run)
    except BaseException:
        if cast_run is not None:
            cast_run.abort()
        raise


def _run_admitted(
    ctx: PipelineRunContext,
    decision: RouteDecision,
    static: MixedStaticPlan,
    generate_outputs: dict[str, pa.Table],
    cast_run: Any,
) -> ExecutionResult:

    publish = ctx.publish
    mode, mode_reason = _output_sink.decide_output_mode(
        stream_chunked_output=publish.stream,
        sink=publish.sink,
        dispatcher_enabled=ctx.chunked_dispatcher_enabled,
        config=ctx.config,
        fidelity_report=ctx.fidelity_report,
        post_validation=ctx.post_validation,
    )
    streamed = mode == "streamed"
    sources = {
        name: (
            ctx.caller_sources[name]
            if streamed
            else _pipeline_sources.materialize_source(ctx.caller_sources[name])
        )
        for name in static.mask_tables
    }

    with publish:
        if streamed:
            publish.open()
        carrier, fk_warnings = _build_carrier(static, sources, generate_outputs)

        mask_outputs: dict[str, pa.Table] = {}
        per_table: dict[str, Any] = {}
        timings: list[Any] = []
        warnings: list[Any] = list(fk_warnings)
        conversion_ms = 0.0
        for name in write_order(ctx.plan, ctx.graph, ctx.table_kinds):
            if name in generate_outputs:
                if streamed:
                    publish.active_sink.write(name, generate_outputs[name])
                continue
            if name not in static.mask_tables:
                continue
            outputs, table_timings, table_ms, table_warnings, table_metrics = (
                _pipeline_auto_chunk.run_auto_chunk(
                    static.mask_configs[name],
                    sources[name],
                    table=name,
                    engine_version=ctx.engine_version,
                    registry=ctx.registry,
                    adapter=ctx.adapter,
                    vault_writer=None,
                    chunk_size_rows=ctx.chunk_size_rows,
                    key_provider=ctx.key_provider,
                    native_threads=ctx.native_threads,
                    dispatcher_enabled=True,
                    sink=publish.active_sink,
                    expected=static.source_facts.get(name),
                    preresolved=carrier if carrier and carrier.table == name else None,
                    cast_run=cast_run,
                )
            )
            mask_outputs.update(outputs)
            timings.extend(table_timings)
            warnings.extend(table_warnings)
            conversion_ms += table_ms
            per_table[name] = table_metrics

        quality_metrics: dict[str, Any] = {
            "mixed_route": {
                "edge": _edge_block(static),
                "output": {"mode": mode, "reason": mode_reason},
                "mask_tables": per_table,
            },
            "execution": {
                "execution_mode": ROUTE_NAME,
                "route_reason": decision.route_reason,
                "eviction": "none",
                "outputs_streamed": streamed,
                "loaded_fully_in_memory": not any(
                    isinstance(s, LazySource) for s in sources.values()
                ),
            },
        }
        # Only the adapter-identity stamp applies here: each mask table's own auto-chunk
        # block rides under `mixed_route`, so the job-level `auto_chunk` stamp (which would
        # report this job as full_frame) is deliberately not requested.
        _pipeline_finalize.stamp_execution_metrics(
            quality_metrics,
            adapter=ctx.adapter,
            resolved_substrate=ctx.resolved_substrate,
            fpe_chunk_count=ctx.fpe_chunk_count,
            route_chunked=False,
            auto_chunk=_pipeline_finalize.AUTO_CHUNK_DEFAULT,
            chunk_size_rows=_pipeline_finalize.CHUNK_SIZE_ROWS_DEFAULT,
            auto_chunk_threshold_rows=_pipeline_finalize.AUTO_CHUNK_THRESHOLD_DEFAULT,
            table_kinds=ctx.table_kinds,
            caller_sources={},
            execution_plan_decision=None,
        )
        if ctx.explain_plan:
            quality_metrics["execution_plan"] = {
                "mode": "chunked",
                "reason": "mixed generate+mask: generation resident, mask tables chunked",
                "rejections": {},
            }
        stamp_out_of_core_declined(quality_metrics, decision.ooc_declined)
        quarantine_token = None
        if cast_run is not None:
            # Seal last: only the publish itself remains.
            quality_metrics["declared_casting"], quarantine_token = cast_run.finish()
        result = ExecutionResult(
            outputs={}
            if streamed
            else stitch_generate_mask_outputs(generate_outputs, mask_outputs),
            timings=tuple(timings),
            boundary_conversion_ms=conversion_ms,
            warnings=tuple(warnings),
            quality_metrics=quality_metrics,
            table_kinds=ctx.table_kinds,
            row_errors=(),
            quarantine_access=quarantine_token,
        )
        publish.commit()
        return result


def _edge_block(static: MixedStaticPlan) -> dict[str, Any] | None:
    edge = static.edge
    if edge is None:
        return None
    return {
        "parent_table": edge.parent_table,
        "parent_column": edge.parent_columns[0],
        "child_table": edge.child_table,
        "child_column": edge.child_columns[0],
        "orphan_policy": str(getattr(edge.orphan_policy, "value", edge.orphan_policy)),
    }

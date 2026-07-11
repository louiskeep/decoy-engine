"""S3 (engine-efficiencies P3) auto-chunk routing: `decide_chunk_route` +
`auto_chunk_stamp`, split out of `_pipeline_routing.py` to hold the 600-LOC
orchestration cap (CLAUDE.md "Engineering best practices") once Sprint B2
added the probe-recovery docstring/params there.

This is layer 2 of the two-layer routing composition `_pipeline_routing`'s
module docstring describes in full: layer 1 (`decide_execution_route`) owns
relationship routing and is reached first; layer 2 (here) owns single-table
chunked-vs-full_frame routing and is reached only when layer 1 did NOT take
the sequential early return. `_pipeline_routing` re-exports both functions
so `run_pipeline` keeps a single `_pipeline_routing.<name>` call surface --
this split is purely a LOC-budget move, not a behavior or API change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

if TYPE_CHECKING:
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = ["auto_chunk_stamp", "decide_chunk_route"]


def decide_chunk_route(
    config: dict[str, Any],
    *,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    substrate: str,
    caller_sources: dict[str, pa.Table],
    auto_chunk_threshold_rows: int,
    explain_plan: bool,
    auto_chunk: bool,
    has_mask_table: bool,
) -> tuple[ExecutionPlan | None, bool]:
    """Auto-chunk go/no-go + explain surfacing.

    ONE `classify_job` call serves both the routing decision and the
    `execution_plan` explain stamp so the explain surface can never
    disagree with what actually ran. The kill switch (`auto_chunk=False`)
    skips classification entirely unless explain asks: a forced
    full-frame run must not depend on planner behavior.

    Returns `(execution_plan_decision, route_chunked)`; `decision` is
    `None` when neither `explain_plan` nor `auto_chunk` asked for a
    classification.
    """
    if not (explain_plan or (auto_chunk and has_mask_table)):
        return None, False

    from decoy_engine.execution._planner import classify_job

    decision = classify_job(
        config,
        plan=plan,
        registry=registry,
        relationship_graph=graph,
        substrate=substrate,
        source_tables=caller_sources,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
    )
    route_chunked = auto_chunk and decision.mode == "chunked"
    return decision, route_chunked


def auto_chunk_stamp(
    *,
    route_chunked: bool,
    auto_chunk: bool,
    chunk_size_rows: int,
    auto_chunk_threshold_rows: int,
    table_kinds: dict[str, str],
    caller_sources: dict[str, pa.Table],
    decision: ExecutionPlan | None,
) -> dict[str, Any]:
    """Build the `quality_metrics["auto_chunk"]` reproducibility block."""
    mask_names = [name for name, kind in table_kinds.items() if kind == "mask"]
    source_rows: int | None = None
    if len(mask_names) == 1 and mask_names[0] in caller_sources:
        source_rows = caller_sources[mask_names[0]].num_rows
    chunk_count: int | None = None
    if route_chunked and source_rows is not None:
        chunk_count = -(-source_rows // chunk_size_rows)
    if route_chunked and decision is not None:
        reason = decision.reason
    elif not auto_chunk:
        reason = "auto_chunk disabled; full-frame path forced by the kill switch"
    elif decision is not None:
        reason = decision.rejections.get(
            "chunked", f"planner selected {decision.mode}: {decision.reason}"
        )
    else:  # unreachable by construction; kept total for safety
        reason = "no routing decision was computed"
    return {
        "mode": "chunked" if route_chunked else "full_frame",
        "chunk_size_rows": chunk_size_rows,
        "threshold_rows": auto_chunk_threshold_rows,
        "source_rows": source_rows,
        "chunk_count": chunk_count,
        "reason": reason,
    }

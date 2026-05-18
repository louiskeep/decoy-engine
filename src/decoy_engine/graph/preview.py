"""Preview policy and shared executor for graph preview runs.

PreviewPolicy defines how execute_preview should handle side-effecting ops,
errors, and row limits. execute_preview runs an ancestor subgraph under that
policy using the same planner (build_preview_plan) and GraphCache path as
the full-run executor in runner.py.

Structural differences from the full-run loop:
- HAS_SIDE_EFFECTS ops are skipped when side_effect_policy="skip"
- FlagPauseSignal is captured rather than re-raised
- row_limit caps are applied at GraphCache.store_from_op() time
- on_error="capture" continues toward the target; "abort" mirrors full-run stop-at-first
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from decoy_engine.exceptions import FlagPauseSignal
from decoy_engine.graph.cache import GraphCache
from decoy_engine.graph.conversion import arrow_columns
from decoy_engine.graph.errors import translate as translate_engine_error
from decoy_engine.graph.events import node_descriptor
from decoy_engine.graph.planner import build_preview_plan
from decoy_engine.graph.types import PreviewResult

if TYPE_CHECKING:
    from decoy_engine.context import ExecutionContext


SideEffectPolicy = Literal["skip", "allow"]
OnError = Literal["capture", "abort"]


@dataclass
class PreviewPolicy:
    """Parameters for a preview execution pass.

    node_id: Target node whose output to sample.
    row_limit: Max rows; clamped to [1, 1000] in execute_preview.
    side_effect_policy:
        "skip"  -- ops with HAS_SIDE_EFFECTS=True are bypassed. If the target
                   itself is a sink, error names the skip reason and explains
                   how to override it.
        "allow" -- all ops run (use only for confirmed dry-run scenarios).
    on_error:
        "capture" -- store error in result, continue toward target node.
        "abort"   -- stop at first error (mirrors full-run behavior).
    """

    node_id: str
    row_limit: int = 50
    side_effect_policy: SideEffectPolicy = "skip"
    on_error: OnError = "capture"


def execute_preview(
    sub_config: dict,
    policy: PreviewPolicy,
    ctx: "ExecutionContext | None" = None,
) -> PreviewResult:
    """Execute the ancestor subgraph under the given preview policy.

    sub_config must already be pruned to the target node's ancestors
    (runner.py's preview_graph does this before calling here).
    """
    from decoy_engine.graph.ops import OPS
    from decoy_engine.graph.registry import native_engine_for

    row_limit = max(1, min(int(policy.row_limit), 1000))
    node_id = policy.node_id

    plan = build_preview_plan(sub_config, node_id)
    graph_cache = GraphCache(plan.consumer_counts)
    overall_start = time.monotonic()
    error_msg: str | None = None
    skipped_nodes: list[str] = []

    for nid in plan.order:
        node = plan.by_id[nid]
        kind = node["kind"]
        op = OPS[kind]
        descriptor = node_descriptor(node)
        node_cfg = dict(node.get("config") or {})
        node_cfg["__preview_row_limit"] = row_limit
        engine = native_engine_for(kind, plan.engine_mode)
        node_cfg["__engine"] = engine
        in_edges = plan.in_edges[nid]

        if policy.side_effect_policy == "skip" and getattr(op, "HAS_SIDE_EFFECTS", False):
            skipped_nodes.append(nid)
            graph_cache._tables[nid] = None
            if nid == node_id:
                error_msg = (
                    f"node {descriptor} skipped: {kind!r} has "
                    f"HAS_SIDE_EFFECTS=True (pass side_effect_policy='allow' "
                    f"to run side-effecting ops in preview)"
                )
                break
            continue

        inputs = [
            graph_cache.consume(e["from"], engine, hold=node_id)
            for e in in_edges
        ]

        try:
            result = op.apply(inputs, node_cfg, ctx)
            if isinstance(result, dict) and getattr(op, "OUTPUT_KIND", None) == "split":
                ports = getattr(op, "OUTPUT_PORTS", ())
                for port in ports:
                    key = f"{nid}.{port}"
                    graph_cache.store_from_op(key, result.get(port), engine, row_limit=row_limit)
                graph_cache._tables[nid] = graph_cache.get_arrow(f"{nid}.pass")
            else:
                graph_cache.store_from_op(nid, result, engine, row_limit=row_limit)
        except FlagPauseSignal as fps:
            error_msg = f"node {descriptor} gate blocked: {fps}"
            graph_cache._tables[nid] = None
            break
        except Exception as exc:
            translated = translate_engine_error(exc, kind, nid)
            error_msg = f"node {descriptor} failed: {translated}"
            graph_cache._tables[nid] = None
            if policy.on_error == "abort" or nid == node_id:
                break

    target_table = graph_cache.get_arrow(node_id)
    elapsed_ms = int((time.monotonic() - overall_start) * 1000)

    if target_table is None:
        return {
            "node_id": node_id,
            "columns": [],
            "rows": [],
            "applied_chain": plan.order,
            "row_count": 0,
            "elapsed_ms": elapsed_ms,
            "error": error_msg or "no data produced",
            "skipped_nodes": skipped_nodes,
        }

    columns = arrow_columns(target_table)
    sliced = target_table.slice(0, row_limit)
    rows = [
        [_jsonable(v) for v in row]
        for row in sliced.to_pandas().itertuples(index=False, name=None)
    ]
    return {
        "node_id": node_id,
        "columns": columns,
        "rows": rows,
        "applied_chain": plan.order,
        "row_count": len(rows),
        "elapsed_ms": elapsed_ms,
        "error": error_msg,
        "skipped_nodes": skipped_nodes,
    }


def _jsonable(v: Any) -> Any:
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v

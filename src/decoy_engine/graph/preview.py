"""Graph preview policy and executor.

Separates preview execution from the full run path so that target
side-effect skipping, row-limit application, and error-capture policy
can be tested and extended without touching the run loop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from decoy_engine.exceptions import FlagPauseSignal
from decoy_engine.graph.cache import GraphCache
from decoy_engine.graph.conversion import arrow_columns
from decoy_engine.graph.errors import translate as translate_engine_error
from decoy_engine.graph.planner import build_plan
from decoy_engine.graph.types import PreviewResult


@dataclass
class PreviewPolicy:
    """Parameters that govern a preview execution run.

    node_id      — target node whose output is returned.
    row_limit    — max rows returned (clamped to 1-1000 at call site).
    skip_targets — when True, target.* ops are never executed so their
                   side effects (file writes, DB inserts) are suppressed.
    """
    node_id: str
    row_limit: int = 50
    skip_targets: bool = True


def execute_preview(
    sub_nodes: list,
    sub_edges: list,
    policy: PreviewPolicy,
    ctx: Any,
    graph_engine_mode: str,
) -> PreviewResult:
    """Run a preview subgraph under policy constraints.

    Uses the same GraphCache and build_plan as _execute_graph so that
    data-path behaviour stays in sync.  Differences from a full run:
      - row_limit applied at every cache write
      - target.* ops skipped when policy.skip_targets is True
      - errors captured in result["error"] rather than stopping the run
      - FlagPauseSignal caught and reported rather than re-raised
      - no node lifecycle events (node_start/ok/error) are emitted
    """
    from decoy_engine.graph.ops import OPS
    from decoy_engine.graph.registry import native_engine_for

    sub_plan = build_plan(sub_nodes, sub_edges)
    by_id = sub_plan.by_id
    sub_order = sub_plan.ordered_ids
    node_id = policy.node_id
    row_limit = policy.row_limit

    gc = GraphCache(sub_plan.consumer_counts, keep_keys={node_id})
    t_start = time.monotonic()
    error_msg: str | None = None

    for nid in sub_order:
        node = by_id[nid]
        kind = node["kind"]

        # Skip target side effects before reading inputs to preserve cache state.
        if policy.skip_targets and kind.startswith("target."):
            gc.store_arrow(nid, None)
            if nid == node_id:
                error_msg = f"node {_node_descriptor(node)} is a target and was skipped"
            continue

        op = OPS[kind]
        node_cfg = dict(node.get("config") or {})
        node_cfg["__preview_row_limit"] = row_limit
        engine = native_engine_for(kind, graph_engine_mode)
        node_cfg["__engine"] = engine
        in_edges = sub_plan.in_edges_by_node.get(nid, [])
        inputs = [gc.read(e["from"], engine) for e in in_edges]

        try:
            result = op.apply(inputs, node_cfg, ctx)
            if isinstance(result, dict) and getattr(op, "OUTPUT_KIND", None) == "split":
                ports = getattr(op, "OUTPUT_PORTS", ())
                gc.write_split(nid, ports, result, engine, row_limit=row_limit)
                # Expose the "pass" port as the direct node output for preview.
                gc.store_arrow(nid, gc.get_arrow(f"{nid}.pass"))
            else:
                gc.write(nid, result, engine, row_limit=row_limit)
        except FlagPauseSignal as fps:
            error_msg = f"node {_node_descriptor(node)} gate blocked: {fps}"
            gc.store_arrow(nid, None)
            if nid == node_id:
                break
        except Exception as exc:
            translated = translate_engine_error(exc, kind, nid)
            error_msg = f"node {_node_descriptor(node)} failed: {translated}"
            gc.store_arrow(nid, None)
            if nid == node_id:
                break

    target_table = gc.get_arrow(node_id)
    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    if target_table is None:
        return {
            "node_id": node_id,
            "columns": [],
            "rows": [],
            "applied_chain": list(sub_order),
            "row_count": 0,
            "elapsed_ms": elapsed_ms,
            "error": error_msg or "no data produced",
        }

    columns = arrow_columns(target_table)
    df_preview = target_table.slice(0, row_limit).to_pandas()
    rows = [
        [_jsonable(v) for v in row]
        for row in df_preview.itertuples(index=False, name=None)
    ]
    return {
        "node_id": node_id,
        "columns": columns,
        "rows": rows,
        "applied_chain": list(sub_order),
        "row_count": len(rows),
        "elapsed_ms": elapsed_ms,
        "error": error_msg,
    }


def _node_descriptor(node: dict) -> str:
    nid = node.get("id", "?")
    kind = node.get("kind", "?")
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return f"{name!r} [id={nid}, kind={kind}]"
    return f"[id={nid}, kind={kind}]"


def _jsonable(v: Any) -> Any:
    """Replace NaN/NaT/etc. with None so row tuples serialize cleanly."""
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v

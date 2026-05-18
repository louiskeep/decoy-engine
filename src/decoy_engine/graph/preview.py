"""graph.preview: preview execution policy and preview runner.

Preview mode runs only the ancestors of a target node, applies a row
limit to every op, captures per-node errors without stopping the run
unless the target node itself fails, and returns PreviewResult instead
of RunResult.

The key difference from a full run is policy, not planner/cache path:
- Both use build_plan() for topo order, in_edges, and consumer_counts.
- Both use GraphCache for inter-node data.
- Both use events.emit_node_start for node start logging.
- Preview injects __preview_row_limit into each node's config so
  side-effecting ops can skip writes and sources can limit scan size.

PreviewPolicy is the single source of truth for preview behavior.
Callers (preview_graph in runner.py, platform adapters) construct a
policy and call run_preview; no preview execution logic lives in the
caller.

Audit Sprint 1.4 - Preview Policy Unification.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decoy_engine.context import ExecutionContext


@dataclass(frozen=True)
class PreviewPolicy:
    """Governs how preview execution differs from a full run.

    target_node_id: The node whose output is being sampled.
    row_limit: Maximum rows to return and the per-op row cap injected
        via __preview_row_limit into every node config. Op authors
        check this key to suppress side effects (file writes, DB
        inserts, cloud uploads) and to limit source scan size.
    skip_side_effects: Informational flag. Side effects are suppressed
        by the __preview_row_limit convention; this field lets the
        platform surface the reason when the target kind starts with
        "target.".
    on_upstream_error: "continue" (default) records the upstream
        failure in error_msg and continues to the next node, passing
        None inputs downstream. This matches the original preview_graph
        best-effort behavior. "stop" halts execution at the first
        failing upstream node. Execution always stops when the target
        node itself fails, regardless of this setting.
    """

    target_node_id: str
    row_limit: int = 50
    skip_side_effects: bool = True
    on_upstream_error: str = "continue"

    def __post_init__(self) -> None:
        if self.on_upstream_error not in ("stop", "continue"):
            raise ValueError(
                f"on_upstream_error must be 'stop' or 'continue', "
                f"got {self.on_upstream_error!r}"
            )
        if self.row_limit < 1:
            raise ValueError(f"row_limit must be >= 1, got {self.row_limit}")

    def node_config_patch(self) -> dict[str, Any]:
        """Keys injected into every node config during preview execution.

        __preview_row_limit signals ops to skip side effects and cap
        their output to the preview row limit at the source level.
        """
        return {"__preview_row_limit": self.row_limit}


def run_preview(
    sub_config: dict[str, Any],
    policy: PreviewPolicy,
    ctx: "ExecutionContext | None",
) -> dict[str, Any]:
    """Execute a validated ancestor subgraph under preview policy.

    sub_config must be a post-validation graph config containing only
    the target node and its ancestors. preview_graph in runner.py is
    responsible for building and validating this subgraph before
    calling here.

    Uses build_plan() for planning (same as _execute_graph) and
    GraphCache for inter-node data. Errors are captured in the
    returned PreviewResult rather than raised; only programming errors
    in the caller (bad sub_config shape) may propagate as exceptions.
    """
    from decoy_engine.context import ExecutionContext
    from decoy_engine.exceptions import FlagPauseSignal
    from decoy_engine.graph.cache import GraphCache
    from decoy_engine.graph.conversion import arrow_columns
    from decoy_engine.graph.errors import translate as translate_engine_error
    from decoy_engine.graph.events import emit_node_start
    from decoy_engine.graph.ops import OPS
    from decoy_engine.graph.planner import build_plan
    from decoy_engine.graph.registry import native_engine_for

    plan = build_plan(sub_config)
    by_id = {n["id"]: n for n in sub_config["nodes"]}
    node_id = policy.target_node_id

    # Pre-truncation row count per node. cache.write_stream truncates
    # at storage time, so by the time we inspect target_table.num_rows
    # we have already lost the original count. Track it here for the
    # ``truncated`` flag below. Computed engine-agnostically from the
    # op result before it lands in the cache.
    pre_trunc_row_counts: dict[str, int] = {}

    # Keep the target (and its split ports) so cache does not evict them
    # at zero consumers before the caller can read the result.
    target_op = OPS.get(by_id[node_id]["kind"]) if node_id in by_id else None
    target_ports: set[str] = set()
    if target_op is not None and getattr(target_op, "OUTPUT_KIND", "") == "split":
        target_ports = {
            f"{node_id}.{port}"
            for port in getattr(target_op, "OUTPUT_PORTS", ())
        }
    cache = GraphCache(plan.consumer_counts, keep={node_id} | target_ports)

    if ctx is None:
        ctx = ExecutionContext()
    log = ctx.logger

    overall_start = time.monotonic()
    error_msg: str | None = None

    # If the target is a side-effecting kind and suppression is active,
    # record the reason now so the platform can surface it even when the
    # op executes successfully (ops suppress their own write via
    # __preview_row_limit; the caller cannot observe that directly).
    skip_reason: str | None = None
    if policy.skip_side_effects and node_id in by_id:
        if by_id[node_id].get("kind", "").startswith("target."):
            skip_reason = "side-effect-suppressed"

    for nid in plan.order:
        node = by_id[nid]
        kind = node["kind"]
        op = OPS[kind]
        node_cfg = dict(node.get("config") or {})
        node_cfg.update(policy.node_config_patch())
        engine = native_engine_for(kind, plan.graph_engine_mode)
        node_cfg["__engine"] = engine

        in_edge_keys = plan.in_edges.get(nid, [])
        rows_in_total = cache.row_sum(in_edge_keys)
        inputs = [cache.consume(k, engine) for k in in_edge_keys]
        descriptor = _node_descriptor(node)

        emit_node_start(log, nid, descriptor, engine, rows_in_total)

        try:
            result = op.apply(inputs, node_cfg, ctx)
            if isinstance(result, dict) and getattr(op, "OUTPUT_KIND", None) == "split":
                ports = getattr(op, "OUTPUT_PORTS", ())
                # Capture the largest port's input row count for truncated
                # detection (target_node, if its OUTPUT_KIND is split, is
                # exposed via the "pass" port -- see set_raw below).
                pass_port_input = result.get("pass") if isinstance(result, dict) else None
                pre_trunc_row_counts[nid] = _result_row_count(pass_port_input)
                cache.write_split(nid, result, ports, engine, row_limit=policy.row_limit)
                # Expose the "pass" port as the direct node output so the
                # target key is always readable regardless of split behavior.
                cache.set_raw(nid, cache.get(f"{nid}.pass"))
            else:
                pre_trunc_row_counts[nid] = _result_row_count(result)
                cache.write_stream(nid, result, engine, row_limit=policy.row_limit)
        except FlagPauseSignal as fps:
            error_msg = f"node {descriptor} gate blocked: {fps}"
            cache.set_raw(nid, None)
            if nid == node_id:
                skip_reason = "gate-blocked"
                break
            if policy.on_upstream_error == "stop":
                break
        except Exception as exc:
            translated = translate_engine_error(exc, kind, nid)
            error_msg = f"node {descriptor} failed: {translated}"
            cache.set_raw(nid, None)
            if nid == node_id or policy.on_upstream_error == "stop":
                break

    target_table = cache.get(node_id)
    elapsed_ms = int((time.monotonic() - overall_start) * 1000)

    if target_table is None:
        return {
            "node_id": node_id,
            "columns": [],
            "rows": [],
            "applied_chain": list(plan.order),
            "row_count": 0,
            "elapsed_ms": elapsed_ms,
            "error": error_msg or "no data produced",
            "truncated": False,
            "skip_reason": skip_reason,
        }

    # truncated reflects "did the op emit more rows than row_limit?" --
    # cache stored at most row_limit, so the post-cache count alone
    # cannot answer that question. pre_trunc_row_counts carries the
    # op's actual output size before any cache-time truncation.
    pre_trunc_rows = pre_trunc_row_counts.get(node_id, target_table.num_rows)
    capped = target_table.slice(0, policy.row_limit)
    columns = arrow_columns(target_table)
    try:
        df_preview = capped.to_pandas()
        rows = [
            [_jsonable(v) for v in row]
            for row in df_preview.itertuples(index=False, name=None)
        ]
    except Exception:
        rows = []

    return {
        "node_id": node_id,
        "columns": columns,
        "rows": rows,
        "applied_chain": list(plan.order),
        "row_count": len(rows),
        "elapsed_ms": elapsed_ms,
        "error": error_msg,
        "truncated": pre_trunc_rows > policy.row_limit,
        "skip_reason": skip_reason,
    }


def _result_row_count(result: Any) -> int:
    """Engine-agnostic row count for an op result.

    Used to capture the pre-truncation row count before the cache stores
    a row-limited slice, so run_preview can later report the truncated
    flag accurately. Handles pandas (``len``), polars (``height``), pyarrow
    (``num_rows``), and ``None``. Returns 0 on unknown shapes rather than
    raising, since the truncated signal is informational only.
    """
    if result is None:
        return 0
    if hasattr(result, "height"):           # polars LazyFrame / DataFrame
        try:
            return int(result.height)
        except Exception:
            return 0
    if hasattr(result, "num_rows"):         # pyarrow Table / RecordBatch
        try:
            return int(result.num_rows)
        except Exception:
            return 0
    try:
        return len(result)                  # pandas DataFrame, list, etc.
    except Exception:
        return 0


def _node_descriptor(node: dict) -> str:
    nid = node.get("id", "?")
    kind = node.get("kind", "?")
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return f"{name!r} [id={nid}, kind={kind}]"
    return f"[id={nid}, kind={kind}]"


def _jsonable(v: Any) -> Any:
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v

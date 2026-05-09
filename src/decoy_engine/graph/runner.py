"""Graph runtime: validate / run / preview entry points.

These are the only symbols `decoy_engine.graph` exposes to callers — see
`graph/__init__.py`. The contract is documented in PIPELINE_GRAPH_GUIDE.md.
"""

import hashlib
import json
import logging
import time
import traceback
from typing import Any

import yaml

from decoy_engine.context import ExecutionContext
from decoy_engine.exceptions import ConfigError, PipelineValidationError
from decoy_engine.graph.topo import topo_order, upstream_subgraph
from decoy_engine.graph.types import (
    NodeRunRecord,
    PreviewResult,
    RunResult,
)
from decoy_engine.internal.validator import GraphConfigValidator, ValidationError


def validate_graph(yaml_text: str) -> None:
    """Validate graph YAML. Raises PipelineValidationError on bad config."""
    config = _load_yaml(yaml_text)
    _quiet_logger = logging.getLogger("decoy_engine.graph.validate")
    if not _quiet_logger.handlers:
        _quiet_logger.addHandler(logging.NullHandler())
    try:
        GraphConfigValidator(_quiet_logger).validate(config)
    except ValidationError as e:
        raise PipelineValidationError(str(e)) from e


def run_graph(
    yaml_text: str, ctx: ExecutionContext | None = None
) -> RunResult:
    """Execute the DAG end-to-end.

    Returns a RunResult with per-node telemetry. On the first node that
    raises, the runner stops, records the failure, and returns
    success=False. Remaining nodes are not executed.
    """
    config = _load_yaml(yaml_text)
    _validate_or_raise(config)

    from decoy_engine.graph.ops import OPS

    edges = config.get("edges") or []
    order = topo_order(config["nodes"], edges)
    by_id = {n["id"]: n for n in config["nodes"]}

    cache: dict[str, Any] = {}
    records: list[NodeRunRecord] = []
    overall_start = time.monotonic()
    success = True

    log = ctx.logger if ctx is not None and ctx.logger is not None else None

    for nid in order:
        node = by_id[nid]
        kind = node["kind"]
        op = OPS[kind]
        node_cfg = dict(node.get("config") or {})
        inputs = [cache[e["from"]] for e in edges if e["to"] == nid]
        descriptor = _node_descriptor(node)

        if log is not None:
            log.info("graph: running node %s", descriptor)

        t0 = time.monotonic()
        try:
            df = op.apply(inputs, node_cfg, ctx)
            cache[nid] = df
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            records.append({
                "node_id": nid,
                "kind": kind,
                "status": "ok",
                "row_count": int(len(df)) if df is not None else None,
                "elapsed_ms": elapsed_ms,
                "error": None,
            })
            if log is not None:
                log.info(
                    "graph: node %s ok rows=%d elapsed=%dms",
                    descriptor,
                    len(df) if df is not None else 0,
                    elapsed_ms,
                )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            records.append({
                "node_id": nid,
                "kind": kind,
                "status": "error",
                "row_count": None,
                "elapsed_ms": elapsed_ms,
                "error": str(exc),
            })
            if log is not None:
                log.error("graph: node %s failed: %s", descriptor, exc)
                log.error(traceback.format_exc())
            success = False
            break

    return {
        "nodes": records,
        "success": success,
        "elapsed_ms": int((time.monotonic() - overall_start) * 1000),
    }


def preview_graph(
    yaml_text: str,
    node_id: str,
    row_limit: int = 50,
    ctx: ExecutionContext | None = None,
) -> PreviewResult:
    """Best-effort sample of `node_id`'s output.

    Walks only the ancestors of `node_id`, applies the row_limit hint to
    sources, and returns the DataFrame at `node_id` capped to `row_limit`.
    Targets do NOT execute their side effect — the dataframe that would
    have been written is returned instead.

    Per the PIPELINE_GRAPH_GUIDE: errors return PreviewResult with
    status-shaped `error` field rather than raising; only validation /
    missing-node errors raise.
    """
    config = _load_yaml(yaml_text)
    _validate_or_raise(config)

    nodes = config["nodes"]
    edges = config.get("edges") or []
    if not any(n["id"] == node_id for n in nodes):
        raise PipelineValidationError(f"node {node_id!r} not in graph")

    row_limit = max(1, min(int(row_limit), 1000))
    sub_order, sub_edges = upstream_subgraph(nodes, edges, node_id)
    by_id = {n["id"]: n for n in nodes}

    from decoy_engine.graph.ops import OPS

    cache: dict[str, Any] = {}
    overall_start = time.monotonic()
    target_df = None
    error_msg: str | None = None

    for nid in sub_order:
        node = by_id[nid]
        kind = node["kind"]
        op = OPS[kind]
        node_cfg = dict(node.get("config") or {})
        # Hint sources/targets to operate in preview mode (capped reads, no writes).
        node_cfg["__preview_row_limit"] = row_limit
        inputs = [cache[e["from"]] for e in sub_edges if e["to"] == nid]

        try:
            df = op.apply(inputs, node_cfg, ctx)
            # Cap downstream DataFrames so a small source doesn't blow up
            # if a transform inflates row count (e.g. cross join).
            if df is not None and len(df) > row_limit:
                df = df.head(row_limit)
            cache[nid] = df
        except Exception as exc:
            error_msg = f"node {_node_descriptor(node)} failed: {exc}"
            cache[nid] = None
            if nid == node_id:
                break

    target_df = cache.get(node_id)
    elapsed_ms = int((time.monotonic() - overall_start) * 1000)

    if target_df is None:
        return {
            "node_id": node_id,
            "columns": [],
            "rows": [],
            "applied_chain": sub_order,
            "row_count": 0,
            "elapsed_ms": elapsed_ms,
            "error": error_msg or "no data produced",
        }

    columns = list(target_df.columns)
    # Convert NaN/NaT to None for JSON-friendliness.
    rows = [
        [_jsonable(v) for v in row] for row in target_df.head(row_limit).itertuples(index=False, name=None)
    ]
    return {
        "node_id": node_id,
        "columns": columns,
        "rows": rows,
        "applied_chain": sub_order,
        "row_count": len(rows),
        "elapsed_ms": elapsed_ms,
        "error": error_msg,
    }


def _load_yaml(yaml_text: str) -> dict:
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("graph config root must be a mapping")
    return data


def _validate_or_raise(config: dict) -> None:
    quiet = logging.getLogger("decoy_engine.graph.runner")
    if not quiet.handlers:
        quiet.addHandler(logging.NullHandler())
    try:
        GraphConfigValidator(quiet).validate(config)
    except ValidationError as e:
        raise PipelineValidationError(str(e)) from e


def _node_descriptor(node: dict) -> str:
    """Format `<name> [id=<id>, kind=<kind>]` for logs.

    `name` is optional in the YAML — drop the leading label when it's
    missing so untagged nodes still log readably. The id+kind tail is
    always present so users can grep logs back to the YAML even when
    two nodes share a name.
    """
    nid = node.get("id", "?")
    kind = node.get("kind", "?")
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return f"{name!r} [id={nid}, kind={kind}]"
    return f"[id={nid}, kind={kind}]"


def _jsonable(v: Any) -> Any:
    """Replace NaN/NaT/etc. with None so the row tuples serialize cleanly."""
    try:
        # pandas NA / numpy nan
        import pandas as pd

        if pd.isna(v):
            return None
    except Exception:
        pass
    return v


def _node_hash(node: dict, upstream_hashes: list[str], row_limit: int) -> str:
    """Composite hash for cache keys (per Q5).

    Process-scoped, used only inside a single preview call right now.
    """
    payload = {
        "kind": node.get("kind"),
        "config": node.get("config") or {},
        "upstream": upstream_hashes,
        "row_limit": row_limit,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()

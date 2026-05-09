"""Graph runtime: validate / run / preview entry points.

These are the only symbols `decoy_engine.graph` exposes to callers — see
`graph/__init__.py`. The contract is documented in PIPELINE_GRAPH_GUIDE.md.

Runtime cache: as of Phase 1 of the polars-duckdb hybrid plan, the runner
caches `pyarrow.Table` between ops and materializes to each op's
`NATIVE_ENGINE` at apply-time. With every op currently declaring
`NATIVE_ENGINE = "pandas"`, behavior is unchanged from the pure-pandas
runner; the substrate is just future-proof. Phases 3 + 4 flip individual ops
to polars / duckdb.

Eviction: cache entries are evicted as soon as their last downstream
consumer reads them. Keeps peak memory bounded by the in-flight working set
rather than the lifetime of the run.
"""

import hashlib
import json
import logging
import time
import traceback
from typing import Any

import pyarrow as pa
import yaml

from decoy_engine.context import ExecutionContext
from decoy_engine.exceptions import ConfigError, PipelineValidationError
from decoy_engine.graph.conversion import (
    arrow_columns,
    arrow_row_count,
    arrow_to_engine,
    engine_to_arrow,
)
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
    from decoy_engine.graph.registry import native_engine_for

    edges = config.get("edges") or []
    nodes = config["nodes"]
    order = topo_order(nodes, edges)
    by_id = {n["id"]: n for n in nodes}
    graph_engine_mode = _resolve_engine_mode(config)

    cache: dict[str, pa.Table] = {}
    remaining = _count_consumers(nodes, edges)
    records: list[NodeRunRecord] = []
    overall_start = time.monotonic()
    success = True

    log = ctx.logger if ctx is not None and ctx.logger is not None else None

    for nid in order:
        node = by_id[nid]
        kind = node["kind"]
        op = OPS[kind]
        node_cfg = dict(node.get("config") or {})
        engine = native_engine_for(kind, graph_engine_mode)
        # Stash the resolved engine so source ops (no upstream input to
        # dispatch on) and any other engine-aware op can branch on it.
        node_cfg["__engine"] = engine

        # Pull upstream outputs out of cache and decrement their consumer
        # count; eviction happens inside _consume.
        in_edges = [e for e in edges if e["to"] == nid]
        inputs = [_consume(cache, remaining, e["from"], engine) for e in in_edges]

        if log is not None:
            log.info("graph: running node %s (%s, engine=%s)", nid, kind, engine)

        t0 = time.monotonic()
        try:
            result = op.apply(inputs, node_cfg, ctx)
            table = engine_to_arrow(result, engine) if result is not None else None
            cache[nid] = table
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            records.append({
                "node_id": nid,
                "kind": kind,
                "status": "ok",
                "row_count": arrow_row_count(table),
                "elapsed_ms": elapsed_ms,
                "error": None,
            })
            if log is not None:
                log.info(
                    "graph: %s ok rows=%d elapsed=%dms",
                    nid,
                    arrow_row_count(table),
                    elapsed_ms,
                )
            # Sink with no downstream consumers: evict immediately so memory
            # is reclaimed (its result is empty by convention anyway).
            if remaining.get(nid, 0) == 0:
                cache.pop(nid, None)
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
                log.error("graph: %s failed: %s", nid, exc)
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
    from decoy_engine.graph.registry import native_engine_for

    graph_engine_mode = _resolve_engine_mode(config)

    # Preview mode does NOT evict the target node's cache (we still need to
    # serialize it after the run). Eviction for upstream-of-target nodes is
    # safe — they have downstream consumers that read them along the way.
    cache: dict[str, pa.Table] = {}
    sub_node_set = set(sub_order)
    sub_nodes = [n for n in nodes if n["id"] in sub_node_set]
    remaining = _count_consumers(sub_nodes, sub_edges)
    overall_start = time.monotonic()
    target_table: pa.Table | None = None
    error_msg: str | None = None

    for nid in sub_order:
        node = by_id[nid]
        kind = node["kind"]
        op = OPS[kind]
        node_cfg = dict(node.get("config") or {})
        node_cfg["__preview_row_limit"] = row_limit
        engine = native_engine_for(kind, graph_engine_mode)
        node_cfg["__engine"] = engine
        in_edges = [e for e in sub_edges if e["to"] == nid]
        # In preview mode, do NOT evict the target node — its output is
        # what the caller will serialize. Eviction for non-target upstreams
        # behaves as in run_graph.
        inputs = [
            _consume(cache, remaining, e["from"], engine, hold=node_id)
            for e in in_edges
        ]

        try:
            result = op.apply(inputs, node_cfg, ctx)
            table = engine_to_arrow(result, engine) if result is not None else None
            # Cap downstream tables so a small source doesn't blow up if a
            # transform inflates row count (e.g. cross join).
            if table is not None and table.num_rows > row_limit:
                table = table.slice(0, row_limit)
            cache[nid] = table
        except Exception as exc:
            error_msg = f"node {nid!r} ({kind}) failed: {exc}"
            cache[nid] = None
            if nid == node_id:
                break

    target_table = cache.get(node_id)
    elapsed_ms = int((time.monotonic() - overall_start) * 1000)

    if target_table is None:
        return {
            "node_id": node_id,
            "columns": [],
            "rows": [],
            "applied_chain": sub_order,
            "row_count": 0,
            "elapsed_ms": elapsed_ms,
            "error": error_msg or "no data produced",
        }

    columns = arrow_columns(target_table)
    # Materialize to pandas at the boundary — the UI consumes a JSON-shaped
    # list-of-lists. This is the canonical preview boundary regardless of
    # which engine produced the table (Phase 5 codifies this).
    df_preview = target_table.slice(0, row_limit).to_pandas()
    rows = [
        [_jsonable(v) for v in row]
        for row in df_preview.itertuples(index=False, name=None)
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


def _count_consumers(nodes: list[dict], edges: list[dict]) -> dict[str, int]:
    """Per node, count how many downstream edges consume its output.

    Used by the cache for eager eviction: when a node's count hits zero,
    its cache entry is released for GC.
    """
    counts: dict[str, int] = {n["id"]: 0 for n in nodes}
    for e in edges:
        # Self-loops are forbidden by the validator; defensive count anyway.
        if e["from"] == e["to"]:
            continue
        if e["from"] in counts:
            counts[e["from"]] += 1
    return counts


def _consume(
    cache: dict[str, pa.Table],
    remaining: dict[str, int],
    node_id: str,
    engine: str,
    hold: str | None = None,
) -> Any:
    """Read upstream output, decrement consumer count, evict at zero.

    `hold` is a node id whose cache entry must survive eviction (preview
    mode pins the target node's entry so the caller can serialize it after
    the run).
    """
    table = cache.get(node_id)
    if table is None:
        return None
    if node_id in remaining:
        remaining[node_id] -= 1
        if remaining[node_id] <= 0 and node_id != hold:
            del cache[node_id]
    return arrow_to_engine(table, engine)  # type: ignore[arg-type]


def _resolve_engine_mode(config: dict) -> str:
    """Read the graph YAML's top-level `engine:` key.

    Values:
      "pandas" (default) — every op runs on pandas regardless of declared
                           NATIVE_ENGINE. Today's behavior.
      "hybrid"           — respect each op's NATIVE_ENGINE declaration. The
                           dogfood opt-in flag introduced in Phase 4 of the
                           polars-duckdb hybrid plan.

    Unknown values fall back to "pandas" with no error — the validator
    could promote this to a hard reject in a follow-up.
    """
    mode = config.get("engine") or "pandas"
    if mode not in ("pandas", "hybrid"):
        return "pandas"
    return mode


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

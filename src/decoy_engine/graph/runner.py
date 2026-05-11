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
import os
import threading
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
from decoy_engine.graph.errors import translate as translate_engine_error
from decoy_engine.graph.topo import topo_order, upstream_subgraph
from decoy_engine.graph.types import (
    NodeRunRecord,
    PreviewResult,
    RunResult,
)
from decoy_engine.internal.validator import GraphConfigValidator, ValidationError


# Memory-pressure warning threshold — fraction of system RAM that, if a
# pipeline's peak RSS reaches it, triggers a "consider engine: pandas"
# warning in the run logs. Customers on tight EC2 instances see the
# advisory before they actually OOM. Override via env var if a customer
# wants quieter or louder signals (e.g. 0.5 for noisier early warning,
# 0.85 for "only when really tight"). The default of 0.7 lines up with
# the calibration: at 50M rows on a 32 GB box, hybrid uses ~72%.
_MEMORY_WARN_THRESHOLD = float(
    os.environ.get("DECOY_MEMORY_WARN_THRESHOLD", "0.7")
)


class _PeakRSSMonitor:
    """Background thread that polls this process's RSS and tracks peak.

    Fixed 200 ms sample interval — fast enough to catch peaks during op
    execution (where the cross-engine dual-representation cost lands),
    slow enough that the polling overhead is negligible. Daemon thread
    so a runner crash doesn't hang the process.
    """

    def __init__(self) -> None:
        self.peak_rss = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._psutil = None
        try:
            import psutil  # local import — psutil is a hard dep but keeps
                           # the runner module importable in environments
                           # where the lib is being upgraded.
            self._psutil = psutil
        except ImportError:
            self._psutil = None

    def __enter__(self) -> "_PeakRSSMonitor":
        if self._psutil is None:
            return self
        self.peak_rss = self._psutil.Process().memory_info().rss
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _poll(self) -> None:
        process = self._psutil.Process()
        while not self._stop.wait(0.2):
            try:
                rss = process.memory_info().rss
            except self._psutil.NoSuchProcess:
                return
            if rss > self.peak_rss:
                self.peak_rss = rss


def _check_memory_pressure(
    peak_rss_bytes: int,
    graph_engine_mode: str,
    log: Any,
) -> None:
    """If a hybrid-mode run got close to system memory, log an advisory.

    Pandas-mode pipelines under memory pressure get a generic warning;
    hybrid-mode pipelines get the specific "switch to engine: pandas"
    suggestion since that path uses ~2× less peak memory per the Bug 5
    calibration (results.md).
    """
    if log is None or peak_rss_bytes == 0:
        return
    try:
        import psutil
        total_bytes = psutil.virtual_memory().total
    except Exception:
        return
    fraction = peak_rss_bytes / total_bytes if total_bytes else 0
    if fraction < _MEMORY_WARN_THRESHOLD:
        return
    peak_gb = peak_rss_bytes / 1024 / 1024 / 1024
    total_gb = total_bytes / 1024 / 1024 / 1024
    if graph_engine_mode == "hybrid":
        log.warning(
            "Pipeline peak memory: %.1f GB (%d%% of %.1f GB system RAM). "
            "For larger jobs on memory-constrained hosts, set "
            "`engine: pandas` in your pipeline YAML to reduce peak memory "
            "by ~2× (trade-off: ~2-3× slower CPU). See "
            "SHARED_ENGINE_ARCHITECTURE.md.",
            peak_gb, int(fraction * 100), total_gb,
        )
    else:
        log.warning(
            "Pipeline peak memory: %.1f GB (%d%% of %.1f GB system RAM). "
            "Job is memory-tight; consider running on a larger instance.",
            peak_gb, int(fraction * 100), total_gb,
        )


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
    result, _cache = _execute_graph(config, ctx)
    return result


def execute_graph_capture(
    yaml_text: str,
    ctx: ExecutionContext | None = None,
    keep_nodes: list[str] | None = None,
) -> tuple[RunResult, dict[str, pa.Table]]:
    """Run a graph and return both the telemetry record AND the output
    cache for nodes named in `keep_nodes`.

    Used by `sub_pipeline` and the iterator ops to capture sub-graph
    output and pipe it into the parent graph. Public-but-quiet: not in
    `decoy_engine.__init__.__all__` because the caller has to know what
    to do with `pa.Table` values, which is internal contract knowledge.

    `keep_nodes=None` keeps nothing (equivalent to `run_graph`).
    """
    config = _load_yaml(yaml_text)
    _validate_or_raise(config)
    result, cache = _execute_graph(config, ctx, keep_nodes=keep_nodes)
    return result, cache


def _execute_graph(
    config: dict,
    ctx: ExecutionContext | None,
    keep_nodes: list[str] | None = None,
) -> tuple[RunResult, dict[str, pa.Table]]:
    """Internal: the actual node-by-node execution loop.

    Returns the telemetry result plus a dict of `{node_id: pa.Table}` for
    nodes the caller wants kept past natural eviction. `run_graph` passes
    `keep_nodes=None`; sub_pipeline / iterator pass the IDs of the nodes
    whose output flows into the parent graph.
    """
    from decoy_engine.graph.ops import OPS
    from decoy_engine.graph.registry import native_engine_for

    edges = config.get("edges") or []
    nodes = config["nodes"]
    order = topo_order(nodes, edges)
    by_id = {n["id"]: n for n in nodes}
    graph_engine_mode = _resolve_engine_mode(config)

    cache: dict[str, pa.Table] = {}
    remaining = _count_consumers(nodes, edges)
    keep_set = set(keep_nodes or [])
    # Bump consumer counts for nodes the caller wants to keep so the
    # eviction logic doesn't reclaim them before we return.
    for k in keep_set:
        remaining[k] = remaining.get(k, 0) + 1
    records: list[NodeRunRecord] = []
    overall_start = time.monotonic()
    success = True

    log = ctx.logger if ctx is not None and ctx.logger is not None else None

    # Track peak RSS across the run; advise the customer if we hit
    # memory pressure (see _check_memory_pressure docstring).
    monitor = _PeakRSSMonitor()
    monitor.__enter__()

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
        descriptor = _node_descriptor(node)

        if log is not None:
            log.info("graph: running node %s (engine=%s)", descriptor, engine)

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
                    "graph: node %s ok rows=%d elapsed=%dms",
                    descriptor,
                    arrow_row_count(table),
                    elapsed_ms,
                )
            # Sink with no downstream consumers: evict immediately so memory
            # is reclaimed (its result is empty by convention anyway).
            if remaining.get(nid, 0) == 0:
                cache.pop(nid, None)
        except Exception as exc:
            translated = translate_engine_error(exc, kind, nid)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            records.append({
                "node_id": nid,
                "kind": kind,
                "status": "error",
                "row_count": None,
                "elapsed_ms": elapsed_ms,
                "error": str(translated),
            })
            if log is not None:
                log.error("graph: node %s failed: %s", descriptor, translated)
                log.error(traceback.format_exc())
            success = False
            break

    monitor.__exit__(None, None, None)
    _check_memory_pressure(monitor.peak_rss, graph_engine_mode, log)

    result: RunResult = {
        "nodes": records,
        "success": success,
        "elapsed_ms": int((time.monotonic() - overall_start) * 1000),
    }
    # Cache contains whatever survived eviction. For `run_graph` callers
    # this is empty (every consumer has read). For `execute_graph_capture`
    # callers the kept nodes are still present because we bumped their
    # consumer counts above.
    kept_cache = {k: cache[k] for k in keep_set if k in cache}
    return result, kept_cache


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
            translated = translate_engine_error(exc, kind, nid)
            error_msg = f"node {_node_descriptor(node)} failed: {translated}"
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
      "hybrid" (default, since Phase 8 of the polars-duckdb hybrid plan):
                           respect each op's NATIVE_ENGINE declaration.
                           Source / sink ops run on DuckDB; relational ops
                           run on Polars; mask / generate / run_storm stay
                           on pandas.
      "pandas"           — opt-out: force every op through its pandas
                           fallback regardless of declaration. The safety
                           hatch for one release cycle. After that, Phase 9
                           (cleanup) removes pandas fallbacks and this flag
                           becomes a no-op.

    Unknown values fall back to the default with no error.
    """
    mode = config.get("engine") or "hybrid"
    if mode not in ("pandas", "hybrid"):
        return "hybrid"
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

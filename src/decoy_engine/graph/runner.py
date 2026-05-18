"""Graph runtime: validate / run / preview entry points.

These are the only symbols `decoy_engine.graph` exposes to callers -- see
`graph/__init__.py`. The contract is documented in PIPELINE_GRAPH_GUIDE.md.

Execution planning: the runner delegates pre-execution graph structure
computation to `graph.planner.build_plan`. The planner computes topo
ordering, in-edge indexing, consumer counts, and engine mode resolution
once before the node-execution loop; see graph/planner.py for details.

Runtime cache: inter-node data is stored as `pyarrow.Table` instances in a
`graph.cache.GraphCache`. The cache converts to each op's declared
`NATIVE_ENGINE` at apply-time and converts the result back to Arrow before
caching. File and cloud source/target ops declare `NATIVE_ENGINE = "duckdb"`.
Transform ops such as `mask` and `generate` declare `NATIVE_ENGINE = "pandas"`
because their strategies use per-row Python callbacks. When the graph-level
`engine:` key is set to `"pandas"`, all ops are forced to pandas regardless
of their declaration. The default `engine: "hybrid"` respects each op's own
declaration.

Eviction: cache entries are evicted as soon as their last downstream
consumer reads them. GraphCache tracks consumer counts and evicts
automatically on the last `consume()` call for each key.

Split ops: ops with OUTPUT_KIND="split" (e.g. `if`) return a dict of port
name to DataFrame. The runner stores each port under `"node_id.port"` keys
in the cache via `GraphCache.write_split`. Downstream edges use the
`"node_id.port"` notation in their `from` field to consume a specific port.

Node lifecycle events: node start, finish, and failure events are built by
`graph.events` helpers. The runner loop calls `emit_node_start`,
`make_node_ok_record` / `emit_node_ok`, and `make_node_error_record` /
`emit_node_error` instead of constructing records and log calls inline.

Preview: `preview_graph` delegates to `graph.preview.run_preview` with a
`PreviewPolicy`. Both preview and full run share the same `build_plan`
call and `GraphCache`; preview behavior cannot silently drift from
full-run behavior because both paths use the same plan builder.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any

import pyarrow as pa
import yaml

from decoy_engine.context import (
    ExecutionContext,
    emit_lineage,
)
from decoy_engine.exceptions import ConfigError, FlagPauseSignal, PipelineValidationError
from decoy_engine.graph.errors import translate as translate_engine_error
from decoy_engine.graph.types import (
    NodeRunRecord,
    PreviewResult,
    RunResult,
)
from decoy_engine.internal.validator import GraphConfigValidator, ValidationError


_MEMORY_WARN_THRESHOLD = float(
    os.environ.get("DECOY_MEMORY_WARN_THRESHOLD", "0.7")
)


class _PeakRSSMonitor:
    """Background thread that polls this process's RSS and tracks peak.

    Fixed 200 ms sample interval -- fast enough to catch peaks during op
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
            import psutil
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
            "by ~2x (trade-off: ~2-3x slower CPU). See "
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
    """Validate graph YAML. Raises PipelineValidationError on bad config.

    The raised exception carries the optional ``path`` and ``code``
    attributes so a platform caller can map the failure back to a
    specific node / inspector field instead of string-parsing the
    message. New callers should prefer :func:`validate_graph_full`
    which returns a multi-message ``ValidationResult`` instead of
    raising; this raise-style entry stays for backward compatibility.
    """
    config = _load_yaml(yaml_text)
    _quiet_logger = logging.getLogger("decoy_engine.graph.validate")
    if not _quiet_logger.handlers:
        _quiet_logger.addHandler(logging.NullHandler())
    try:
        GraphConfigValidator(_quiet_logger).validate(config)
    except ValidationError as e:
        raise PipelineValidationError(
            str(e), path=e.path, code=getattr(e, "code", None),
        ) from e


def validate_graph_full(yaml_text: str):
    """Validate graph YAML and return a non-raising :class:`ValidationResult`.

    Unlike :func:`validate_graph`, this never raises on validation
    failure. Instead it returns a ``ValidationResult`` with structured
    ``errors`` and ``warnings`` lists; callers can render every problem
    at once and map each to a UI field via the stable ``code`` string.

    YAML parse errors (the input isn't valid YAML at all) still raise
    as ``decoy_engine.exceptions.ConfigError`` -- they're an upstream
    problem, not a validation outcome. Use a try/except at the call
    site if needed.

    The validator currently stops at the first error (legacy behavior
    of the underlying ``GraphConfigValidator``). Multi-error reporting
    will be enabled per-subject in follow-up R2.2 work as each
    validator is migrated to non-raising form.
    """
    from decoy_engine.validation_result import CODES, ValidationResult

    result = ValidationResult()
    config = _load_yaml(yaml_text)
    _quiet_logger = logging.getLogger("decoy_engine.graph.validate")
    if not _quiet_logger.handlers:
        _quiet_logger.addHandler(logging.NullHandler())
    try:
        GraphConfigValidator(_quiet_logger).validate(config)
    except ValidationError as e:
        raw = getattr(e, "raw_message", None) or str(e)
        result.add_error(
            code=getattr(e, "code", None) or CODES.UNTAGGED,
            message=raw,
            path=getattr(e, "path", None),
        )
        return result
    result.normalized_config = config
    return result


def run_graph(
    yaml_text: str, ctx: ExecutionContext | None = None
) -> RunResult:
    """Execute the DAG end-to-end.

    Returns a RunResult with per-node telemetry. On the first node that
    raises, the runner stops, records the failure, and returns
    success=False. Remaining nodes are not executed.

    FlagPauseSignal is re-raised without wrapping -- the platform runner
    catches it to transition the job to review_pending.
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
    output and pipe it into the parent graph.
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
    from decoy_engine.graph.cache import GraphCache
    from decoy_engine.graph.events import (
        emit_node_error,
        emit_node_ok,
        emit_node_start,
        make_node_error_record,
        make_node_ok_record,
    )
    from decoy_engine.graph.ops import OPS
    from decoy_engine.graph.planner import build_plan
    from decoy_engine.graph.registry import native_engine_for

    nodes = config["nodes"]
    by_id = {n["id"]: n for n in nodes}
    plan = build_plan(config)
    graph_engine_mode = plan.graph_engine_mode

    keep_set = set(keep_nodes or [])
    cache = GraphCache(plan.consumer_counts, keep=keep_set)
    records: list[NodeRunRecord] = []
    overall_start = time.monotonic()
    success = True

    # The runner always works with a real ExecutionContext so ops can call
    # `ctx.export()` regardless of caller. External callers that pass None
    # still get the same behavior; the exports just don't escape this scope.
    if ctx is None:
        ctx = ExecutionContext()

    log = ctx.logger

    with _PeakRSSMonitor() as monitor:
        # One-shot lineage emission: classify every node by its kind family
        # (source.* -> source, target.* -> output, everything else -> transform)
        # and tag with the engine-resolved kind so the UI can route an icon.
        # Done before the run loop so a node failure mid-run still leaves a
        # complete lineage record for the timeline.
        for nid in plan.order:
            node = by_id[nid]
            kind = node["kind"]
            if kind.startswith("source."):
                emit_lineage(log, "source", nid, kind)
            elif kind.startswith("target."):
                emit_lineage(log, "output", nid, kind)
            else:
                emit_lineage(log, "transform", nid, kind)

        for nid in plan.order:
            node = by_id[nid]
            kind = node["kind"]
            op = OPS[kind]
            node_cfg = dict(node.get("config") or {})
            engine = native_engine_for(kind, graph_engine_mode)
            node_cfg["__engine"] = engine

            # Resolve `${nodes.<id>.<key>}` tokens against exports captured by
            # already-completed nodes. Other scopes (var/env/trigger/storm) are
            # resolved by the platform before the YAML reaches the engine; this
            # one is engine-only because the values don't exist until upstream
            # ops have run.
            try:
                node_cfg = _resolve_node_exports(node_cfg, ctx._exports, nid)
            except _NodeExportResolutionError as exc:
                records.append(make_node_error_record(nid, kind, 0, str(exc)))
                if log is not None:
                    log.error("graph: node %s failed: %s", _node_descriptor(node), exc)
                success = False
                break

            # Pre-indexed incoming edge "from" keys from the plan; avoids an
            # O(nodes*edges) scan per node. The plan is built once before the loop.
            in_edge_keys = plan.in_edges.get(nid, [])
            # ``step_name`` is what the platform's JobLogger writes into the
            # STEP column of every narrative line emitted while this node is
            # the open step, AND what the reporting UI's pill timeline keys
            # on. Using the node id (not the kind) means every node is its
            # own pill, not one pill per kind -- which matters for graphs
            # with multiple mask / target nodes.
            step_name = nid
            # rows_in: sum of upstream row counts read BEFORE consuming
            # (cache.row_sum does not evict) so we see the full Arrow tables.
            # Source ops with no upstream input naturally land at 0.
            rows_in_total = cache.row_sum(in_edge_keys)
            inputs = [cache.consume(k, engine) for k in in_edge_keys]
            descriptor = _node_descriptor(node)

            emit_node_start(log, step_name, descriptor, engine, rows_in_total)

            # Per-node config snapshot in the narrative log so the Task History
            # Nodes tab shows what each node was configured with at run time
            # (predicate, columns, output path, etc.) -- the boundary emit alone
            # leaves the bucket nearly empty for ops that don't log internally.
            # Emitted AFTER step start so JobLogger tags the line with this
            # node's step name.
            if log is not None and node_cfg:
                log.info(_summarize_node_config(kind, node_cfg))

            t0 = time.monotonic()
            ctx._current_node_id = nid
            try:
                result = op.apply(inputs, node_cfg, ctx)
                # Split-output ops (Item 21 IF / FLAG routers): each declared
                # OUTPUT_PORT lands in its own cache slot keyed `<nid>.<port>`
                # so downstream consumers can read a single port. Single-output
                # ops take the regular `cache[nid]` path.
                if isinstance(result, dict) and getattr(op, "OUTPUT_KIND", None) == "split":
                    ports = getattr(op, "OUTPUT_PORTS", ())
                    total_rows = cache.write_split(nid, result, ports, engine)
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    records.append(make_node_ok_record(
                        nid, kind, total_rows, elapsed_ms, ctx._exports.get(nid),
                    ))
                    emit_node_ok(
                        log, step_name, descriptor, rows_in_total,
                        total_rows, elapsed_ms, is_split=True,
                    )
                else:
                    rows_out = cache.write_stream(nid, result, engine)
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    records.append(make_node_ok_record(
                        nid, kind, rows_out, elapsed_ms, ctx._exports.get(nid),
                    ))
                    emit_node_ok(
                        log, step_name, descriptor, rows_in_total,
                        rows_out, elapsed_ms,
                    )
            except FlagPauseSignal:
                # Item 21 controlled pause -- not a failure. Let the platform
                # runner handle it (creates a JobReview row, transitions the
                # job to review_pending). No emit_step(error) because the
                # phase isn't done yet; the resumed run will continue from
                # the gate.
                raise
            except Exception as exc:
                translated = translate_engine_error(exc, kind, nid)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                records.append(make_node_error_record(
                    nid, kind, elapsed_ms, str(translated),
                    exports=ctx._exports.get(nid),
                    error_code=getattr(translated, "code", None),
                    error_path=getattr(translated, "path", None),
                ))
                emit_node_error(
                    log, step_name, descriptor, rows_in_total,
                    exc, translated, nid, elapsed_ms,
                )
                success = False
                break
            finally:
                ctx._current_node_id = None

    _check_memory_pressure(monitor.peak_rss, graph_engine_mode, log)

    result: RunResult = {
        "nodes": records,
        "success": success,
        "elapsed_ms": int((time.monotonic() - overall_start) * 1000),
    }
    return result, cache.kept()


def preview_graph(
    yaml_text: str,
    node_id: str,
    row_limit: int = 50,
    ctx: ExecutionContext | None = None,
) -> PreviewResult:
    """Best-effort sample of `node_id`'s output.

    Walks only the ancestors of `node_id`, applies the row_limit hint to
    sources, and returns the DataFrame at `node_id` capped to `row_limit`.
    Targets do NOT execute their side effect -- the data that would have
    been written is returned instead. `result["skip_reason"]` is set to
    "side-effect-suppressed" so the platform can explain why no write
    occurred.

    Per the PIPELINE_GRAPH_GUIDE: errors return PreviewResult with
    status-shaped `error` field rather than raising; only validation /
    missing-node errors raise.

    Validation is scoped to the target node + its ancestors. If the
    pipeline is broken downstream of `node_id` (or in some other branch),
    sampling here still works -- the user can grab data out of any node
    whose upstream is well-formed.

    Uses build_plan() for planning and GraphCache for inter-node data,
    sharing the same planner/cache path as run_graph. Preview behavior
    cannot silently drift from full-run behavior because both paths use
    the same plan builder (Sprint 1.4).
    """
    from decoy_engine.graph.preview import PreviewPolicy, run_preview

    config = _load_yaml(yaml_text)
    _validate_top_level_or_raise(config)

    nodes = config["nodes"]
    edges = config.get("edges") or []
    if not any(isinstance(n, dict) and n.get("id") == node_id for n in nodes):
        raise PipelineValidationError(f"node {node_id!r} not in graph")

    needed = _ancestor_node_ids_safe(nodes, edges, node_id)
    sub_config = {
        **config,
        "nodes": [n for n in nodes if isinstance(n, dict) and n.get("id") in needed],
        "edges": [
            e for e in edges
            if isinstance(e, dict)
            and isinstance(e.get("from"), str)
            and isinstance(e.get("to"), str)
            and e["from"].split(".", 1)[0] in needed
            and e["to"] in needed
        ],
    }
    _validate_or_raise(sub_config)

    policy = PreviewPolicy(
        target_node_id=node_id,
        row_limit=max(1, min(int(row_limit), 1000)),
    )
    return run_preview(sub_config, policy, ctx)


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
        raise PipelineValidationError(str(e), path=e.path) from e


def _validate_top_level_or_raise(config: dict) -> None:
    """Cheap structural check used before extracting a preview subgraph.

    Only checks the shape needed to walk edges/nodes safely. Per-node and
    cardinality checks happen later against the subgraph so that broken
    downstream nodes do not block sampling at an upstream node.
    """
    if config.get("mode") != "graph":
        raise PipelineValidationError(
            f"top-level 'mode' must be 'graph' (got {config.get('mode')!r})"
        )
    nodes = config.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise PipelineValidationError("'nodes' must be a non-empty list")
    edges = config.get("edges")
    if edges is not None and not isinstance(edges, list):
        raise PipelineValidationError("'edges' must be a list")


def _ancestor_node_ids_safe(
    nodes: list, edges: list, target: str
) -> set[str]:
    """Walk upward from `target` collecting ancestors, tolerant of
    malformed nodes/edges in unrelated parts of the graph.

    Returns the set of node IDs comprising `target` plus every ancestor
    reachable through well-formed edges.
    """
    valid_ids = {
        n.get("id") for n in nodes
        if isinstance(n, dict) and isinstance(n.get("id"), str)
    }
    in_edges: dict[str, list[str]] = {}
    for e in edges or []:
        if not isinstance(e, dict):
            continue
        src = e.get("from")
        dst = e.get("to")
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        in_edges.setdefault(dst, []).append(src.split(".", 1)[0])

    needed: set[str] = set()
    stack = [target]
    while stack:
        nid = stack.pop()
        if nid in needed or nid not in valid_ids:
            continue
        needed.add(nid)
        stack.extend(in_edges.get(nid, []))
    return needed


def _node_descriptor(node: dict) -> str:
    nid = node.get("id", "?")
    kind = node.get("kind", "?")
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return f"{name!r} [id={nid}, kind={kind}]"
    return f"[id={nid}, kind={kind}]"


# Keys to redact in the per-node config summary line. Anything name-matching
# (case-insensitive) gets `***` in the emitted log. Keeps the Task History
# Nodes-tab read-out useful without leaking credentials into Job.log.
_REDACT_KEYS = {"password", "secret", "token", "api_key", "apikey", "auth"}


def _summarize_node_config(kind: str, cfg: dict) -> str:
    """Return a short per-node config summary for the narrative log.

    Emitted right after each step starts so the Task History Nodes tab
    always has at least one informative line per node -- the step boundary
    emit alone isn't enough for users to see what the node actually did.

    Keep this terse: the resolved config dict already lives on
    JobNodeRun.config for the full picture. This line is a glance value.
    Secrets are redacted by key name."""
    if not isinstance(cfg, dict) or not cfg:
        return f"config: (no config)"
    parts: list[str] = []
    for k, v in cfg.items():
        if k.startswith("_"):
            continue
        key_l = str(k).lower()
        if any(rk in key_l for rk in _REDACT_KEYS):
            parts.append(f"{k}=***")
            continue
        if isinstance(v, (dict, list)):
            # Lists / dicts: show the length so a "mask: 12 columns" still
            # reads at a glance without flooding the line with structure.
            kind_word = "keys" if isinstance(v, dict) else "items"
            parts.append(f"{k}=<{len(v)} {kind_word}>")
        elif isinstance(v, str) and len(v) > 80:
            parts.append(f"{k}={v[:77]!r}...")
        else:
            parts.append(f"{k}={v!r}")
        if len(parts) >= 6:
            parts.append("...")
            break
    return f"config: " + ", ".join(parts)


def _jsonable(v: Any) -> Any:
    """Replace NaN/NaT/etc. with None so the row tuples serialize cleanly."""
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except Exception:
        pass
    return v


# Engine-side resolver for `${nodes.<id>.<key>[.<sub>...]}` tokens. Other
# scopes (var/env/trigger/storm/iteration) are resolved platform-side before
# the YAML reaches the engine. This scope must be resolved live because the
# values come from already-completed upstream ops.
_NODE_TOKEN_RE = re.compile(r"\$\{nodes\.([a-zA-Z0-9_-]+)\.([a-zA-Z_][\w.]*)\}")


class _NodeExportResolutionError(Exception):
    """Raised when a `${nodes.X.Y}` token can't be resolved.

    The runner catches this, records the failing node with an actionable
    error message, and stops the pipeline."""


def _resolve_node_exports(
    cfg: Any,
    exports: dict[str, dict[str, Any]],
    current_node_id: str,
) -> Any:
    """Walk cfg and substitute `${nodes.X.Y}` tokens against `exports`.

    Returns a new structure; cfg is not mutated. Raises
    `_NodeExportResolutionError` for unknown ids / keys (which usually means
    a forward reference or a typo)."""
    return _walk_for_exports(cfg, exports, current_node_id)


def _walk_for_exports(
    node: Any,
    exports: dict[str, dict[str, Any]],
    current_node_id: str,
) -> Any:
    if isinstance(node, dict):
        return {k: _walk_for_exports(v, exports, current_node_id) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk_for_exports(v, exports, current_node_id) for v in node]
    if isinstance(node, str):
        return _replace_node_exports_in_string(node, exports, current_node_id)
    return node


def _replace_node_exports_in_string(
    s: str,
    exports: dict[str, dict[str, Any]],
    current_node_id: str,
) -> Any:
    # Whole-string token preserves type (int / float / list / dict stay
    # their native shape). Partial substitution coerces to str.
    full = _NODE_TOKEN_RE.fullmatch(s)
    if full is not None:
        return _resolve_one_node_export(
            full.group(1), full.group(2), exports, current_node_id
        )

    def replace(match: re.Match[str]) -> str:
        return str(_resolve_one_node_export(
            match.group(1), match.group(2), exports, current_node_id
        ))

    return _NODE_TOKEN_RE.sub(replace, s)


def _resolve_one_node_export(
    node_id: str,
    key: str,
    exports: dict[str, dict[str, Any]],
    current_node_id: str,
) -> Any:
    if node_id == current_node_id:
        raise _NodeExportResolutionError(
            f"node {current_node_id!r} references its own exports via "
            f"${{nodes.{node_id}.{key}}} -- exports are only readable from "
            f"downstream nodes"
        )
    if node_id not in exports:
        raise _NodeExportResolutionError(
            f"unresolved variable: ${{nodes.{node_id}.{key}}} -- node "
            f"{node_id!r} has not run yet (forward reference or upstream "
            f"failure)"
        )
    cur: Any = exports[node_id]
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                raise _NodeExportResolutionError(
                    f"unresolved variable: ${{nodes.{node_id}.{key}}} -- "
                    f"index {idx} out of range"
                )
        else:
            raise _NodeExportResolutionError(
                f"unresolved variable: ${{nodes.{node_id}.{key}}} -- "
                f"key {part!r} not in {node_id!r}'s exports"
            )
    return cur


def _node_hash(node: dict, upstream_hashes: list[str], row_limit: int) -> str:
    payload = {
        "kind": node.get("kind"),
        "config": node.get("config") or {},
        "upstream": upstream_hashes,
        "row_limit": row_limit,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()

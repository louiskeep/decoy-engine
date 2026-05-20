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


def validate_graph_full(yaml_text: str, *, strict: bool = False):
    """Validate graph YAML and return a non-raising :class:`ValidationResult`.

    Unlike :func:`validate_graph`, this never raises on validation
    failure. Instead it returns a ``ValidationResult`` with structured
    ``errors`` and ``warnings`` lists; callers can render every problem
    at once and map each to a UI field via the stable ``code`` string.

    YAML parse errors (the input isn't valid YAML at all) still raise
    as ``decoy_engine.exceptions.ConfigError`` -- they're an upstream
    problem, not a validation outcome. Use a try/except at the call
    site if needed.

    Each validation stage is tried independently. A top-level structural
    error stops further validation (no safe graph to walk). The nodes
    stage (stage 2, R2.2) collects all per-node errors so a graph with
    multiple bad nodes surfaces every failure in one pass. Edge,
    cardinality, topology, and cross-node stages each capture their first
    failure before moving on. The cross-node semantic checks (format
    consistency, mask column reachability, nodes-ref reachability) run
    independently of each other so all three can surface errors in one
    pass. Within stages 3-8 the first failure still stops that stage.

    ``normalized_config`` is a deep copy of the parsed input with defaults
    applied (e.g. target.file format inferred from the source format). It
    is set only when there are no errors. The original parsed config is
    never mutated.

    ``strict=True`` enables production-mode gating: checks that are
    advisory in lenient mode become blocking errors. Full strict-mode
    coverage (credentials, variable resolution, side-effect policy) is
    Sprint 2 follow-up work; the parameter is accepted now so callers
    can adopt it forward.
    """
    import copy
    from decoy_engine.validation_result import CODES, ValidationResult

    result = ValidationResult()
    config = _load_yaml(yaml_text)
    _quiet_logger = logging.getLogger("decoy_engine.graph.validate")
    if not _quiet_logger.handlers:
        _quiet_logger.addHandler(logging.NullHandler())

    # Deep copy so _validate_file_format_consistency's format back-fill
    # never mutates the locally-parsed config dict. The normalized_config
    # returned to the caller is the deep copy with defaults applied.
    working = copy.deepcopy(config)
    validator = GraphConfigValidator(_quiet_logger)

    def _collect(stage_fn) -> bool:
        """Call stage_fn(). Add its first error to result. Return True iff it passed."""
        try:
            stage_fn()
            return True
        except ValidationError as e:
            result.add_error(
                code=getattr(e, "code", None) or CODES.UNTAGGED,
                message=getattr(e, "raw_message", None) or str(e),
                path=getattr(e, "path", None),
            )
            return False

    # Stage 1: top-level shape. Required before safely extracting nodes/edges.
    if not _collect(lambda: validator._validate_top_level(working)):
        return result

    # Post-stage-1: nodes is a non-empty list, edges is a list or absent.
    nodes = working["nodes"]
    edges = working.get("edges") or []
    kinds = validator._known_kinds()

    # Stage 2: per-node metadata -- collects ALL per-node errors so a graph
    # with multiple bad nodes surfaces all of them in one pass (R2.2).
    node_errors = validator._validate_nodes_collecting(nodes, kinds)
    for _e in node_errors:
        result.add_error(
            code=getattr(_e, "code", None) or CODES.UNTAGGED,
            message=getattr(_e, "raw_message", None) or str(_e),
            path=getattr(_e, "path", None),
        )
    nodes_ok = not node_errors

    # Stages 3-5: graph structure. Each requires the prior stage to pass.
    edges_ok = nodes_ok and _collect(lambda: validator._validate_edges(edges, nodes))
    cardinality_ok = edges_ok and _collect(
        lambda: validator._validate_cardinality(nodes, edges, kinds)
    )
    topology_ok = cardinality_ok and _collect(
        lambda: validator._validate_acyclic(nodes, edges)
    )

    # Stages 6-8: cross-node semantic checks. Each runs independently so
    # all three can surface errors in one pass. All require a sound acyclic
    # graph to walk.
    if topology_ok:
        _collect(lambda: validator._validate_file_format_consistency(nodes, edges, strict=strict))
        _collect(lambda: validator._validate_mask_column_reachability(nodes, edges))
        _collect(lambda: validator._validate_nodes_ref_reachability(nodes, edges))

    # ── Stage 9: column_relationships (Sprint 4, item 4) ──
    #
    # Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;
    # materialize parent pool; child samples with replacement.
    #
    # Validates the top-level column_relationships: block emitted by
    # the platform's FORECAST pipeline build. Independent of stages
    # 6-8 (those check the graph mechanics; this one checks the FK
    # declaration shape). Topology-aware: requires plan order from
    # build_plan, so it runs only when topology_ok is True.
    if topology_ok:
        _collect(lambda: _validate_column_relationships(working, strict=strict, result=result))

    if not result.errors:
        result.normalized_config = working
    return result


def _validate_column_relationships(
    config: dict,
    *,
    strict: bool,
    result,
) -> None:
    """Validate the top-level ``column_relationships:`` block.

    Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;
    materialize parent pool; child samples with replacement.

    Per-entry checks (all error-level unless noted):

      - fk.unknown_node        : parent.node or child.node not in graph
      - fk.unknown_column      : node exists but the referenced column
                                 doesn't appear in the node's config or
                                 (for source nodes) the source's
                                 declared schema. Source nodes without
                                 a `columns` config get a carve-out
                                 because their schema is the file's
                                 actual columns, which the engine only
                                 knows at runtime.
      - fk.parent_after_child  : parent appears AFTER child in
                                 plan.order; refuses to run.
      - fk.self_reference      : parent.node == child.node; out of V1
                                 scope (V2 will lift via SDV self-loop
                                 handling).
      - fk.parallel_branches   : no topological path from parent to
                                 child. Advisory in lenient mode
                                 (cache pinning handles parallel-branch
                                 survival); error in strict mode.
      - fk.nondeterministic_mask : a mask op participates in an FK and
                                 uses a non-deterministic strategy
                                 (redact, shuffle, truncate). Advisory
                                 (severity=warning) by default so
                                 operators shipping a one-off scrub
                                 don't hit a hard wall when roundtrip
                                 joinability isn't required. Set the
                                 env var ``DECOY_FK_STRICT_DETERMINISM=1``
                                 (or ``true`` / ``yes``) to restore the
                                 hard reject; that's the right knob for
                                 long-lived analytics pipelines where
                                 cross-run join stability matters.

    Skips silently when no column_relationships block exists.
    """
    import os
    from decoy_engine.graph.planner import build_plan
    from decoy_engine.validation_result import CODES

    rels = config.get("column_relationships")
    if not rels:
        return
    if not isinstance(rels, list):
        result.add_error(
            code=CODES.FK_UNKNOWN_NODE,
            message="column_relationships must be a list",
            path="column_relationships",
        )
        return

    # plan.order gives topo position; needed for parent-after-child check.
    try:
        plan = build_plan(config)
    except Exception:
        # If the graph won't plan, we can't reason about FK ordering.
        # The topology stage already flagged the structural failure.
        return
    pos_in_order = {nid: idx for idx, nid in enumerate(plan.order)}

    nodes_by_id = {n["id"]: n for n in config.get("nodes") or []}

    # Build the reachability cache lazily; only consulted when emitting
    # fk.parallel_branches.
    _reach_cache: dict[tuple[str, str], bool] = {}
    edges_list = config.get("edges") or []

    def _reachable(from_id: str, to_id: str) -> bool:
        key = (from_id, to_id)
        if key in _reach_cache:
            return _reach_cache[key]
        # BFS over edges. Small graphs in practice; no need for fancier
        # structures.
        visited = {from_id}
        stack = [from_id]
        while stack:
            curr = stack.pop()
            if curr == to_id:
                _reach_cache[key] = True
                return True
            for e in edges_list:
                if isinstance(e, dict) and e.get("from") == curr and e.get("to") not in visited:
                    visited.add(e["to"])
                    stack.append(e["to"])
        _reach_cache[key] = False
        return False

    # Set of deterministic mask strategies. Anything outside breaks FK.
    DETERMINISTIC_MASK_STRATEGIES = frozenset(
        {"hash", "fpe", "faker", "date_shift", "reference"}
    )

    def _column_in_node(node: dict, column: str) -> bool:
        """True if `column` appears in `node`'s config columns mapping.
        For source nodes the schema is the file's actual columns,
        unknowable at validation time, so we treat them as opaque and
        return True. The runtime resolver will catch a true miss at
        execution time and emit fk.unknown_column then."""
        kind = node.get("kind", "")
        if kind.startswith("source."):
            return True
        cfg = node.get("config") or {}
        cols = cfg.get("columns")
        if not isinstance(cols, dict):
            # Unknown shape; be lenient at validation time.
            return True
        return column in cols

    def _mask_strategy_for_column(node: dict, column: str) -> str | None:
        """If `node` is a mask op and `column` is configured, return its
        strategy. Otherwise None (caller skips the determinism check)."""
        if node.get("kind") != "mask":
            return None
        cfg = node.get("config") or {}
        col_spec = (cfg.get("columns") or {}).get(column)
        if not isinstance(col_spec, dict):
            return None
        return col_spec.get("strategy")

    for i, rel in enumerate(rels):
        path = f"column_relationships[{i}]"
        if not isinstance(rel, dict):
            result.add_error(
                code=CODES.FK_UNKNOWN_NODE,
                message="entry must be a mapping",
                path=path,
            )
            continue
        parent = rel.get("parent") or {}
        child = rel.get("child") or {}
        p_node = parent.get("node") if isinstance(parent, dict) else None
        p_col = parent.get("column") if isinstance(parent, dict) else None
        c_node = child.get("node") if isinstance(child, dict) else None
        c_col = child.get("column") if isinstance(child, dict) else None

        if not p_node or not c_node or not p_col or not c_col:
            result.add_error(
                code=CODES.FK_UNKNOWN_NODE,
                message=f"entry missing parent.node / parent.column / child.node / child.column",
                path=path,
            )
            continue

        # Self-reference: V2.
        if p_node == c_node:
            result.add_error(
                code=CODES.FK_SELF_REFERENCE,
                message=f"self-references not supported in V1 (parent and child are both {p_node!r})",
                path=path,
            )
            continue

        # Unknown nodes.
        if p_node not in nodes_by_id:
            result.add_error(
                code=CODES.FK_UNKNOWN_NODE,
                message=f"parent node {p_node!r} not present in graph",
                path=f"{path}.parent.node",
            )
            continue
        if c_node not in nodes_by_id:
            result.add_error(
                code=CODES.FK_UNKNOWN_NODE,
                message=f"child node {c_node!r} not present in graph",
                path=f"{path}.child.node",
            )
            continue

        # Topology: parent must precede child in plan.order.
        if pos_in_order.get(p_node, 0) >= pos_in_order.get(c_node, 0):
            result.add_error(
                code=CODES.FK_PARENT_AFTER_CHILD,
                message=(
                    f"parent {p_node!r} does not precede child {c_node!r} "
                    "in topological order (parent must produce its column "
                    "before child consumes it)"
                ),
                path=path,
            )
            continue

        # Parallel-branch advisory: parent + child both run, but no
        # topological path connects them. Cache pinning keeps both
        # alive so this is fine in practice; surface as a warning in
        # lenient mode + an error in strict mode.
        if not _reachable(p_node, c_node):
            if strict:
                result.add_error(
                    code=CODES.FK_PARALLEL_BRANCHES,
                    message=(
                        f"no topological path from parent {p_node!r} to child {c_node!r}; "
                        "they run on parallel branches (strict mode rejects this)"
                    ),
                    path=path,
                )
            else:
                result.add_warning(
                    code=CODES.FK_PARALLEL_BRANCHES,
                    message=(
                        f"parent {p_node!r} and child {c_node!r} are on parallel branches; "
                        "cache pinning keeps both alive but the relationship is implicit"
                    ),
                    path=path,
                )

        # Column presence (best-effort; source nodes get a carve-out).
        p_node_obj = nodes_by_id[p_node]
        c_node_obj = nodes_by_id[c_node]
        if not _column_in_node(p_node_obj, p_col):
            result.add_error(
                code=CODES.FK_UNKNOWN_COLUMN,
                message=(
                    f"parent column {p_col!r} not declared in parent {p_node!r} config "
                    f"(kind={p_node_obj.get('kind')})"
                ),
                path=f"{path}.parent.column",
            )
        if not _column_in_node(c_node_obj, c_col):
            result.add_error(
                code=CODES.FK_UNKNOWN_COLUMN,
                message=(
                    f"child column {c_col!r} not declared in child {c_node!r} config "
                    f"(kind={c_node_obj.get('kind')})"
                ),
                path=f"{path}.child.column",
            )

        # Mask determinism: both ends, if they're mask ops, must use a
        # deterministic strategy to preserve the FK. Advisory by
        # default (severity=warning) so a one-off run with redact or
        # shuffle on an FK column doesn't hard-fail; set
        # DECOY_FK_STRICT_DETERMINISM=1 to upgrade back to error. The
        # advisory still records the affected column so the platform
        # manifest assembler can hydrate `fk_preservation.advisories`
        # for downstream auditors.
        strict_determinism = (
            os.environ.get("DECOY_FK_STRICT_DETERMINISM", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        for side, node_obj, col_name in (
            ("parent", p_node_obj, p_col),
            ("child",  c_node_obj, c_col),
        ):
            strategy = _mask_strategy_for_column(node_obj, col_name)
            if strategy is not None and strategy not in DETERMINISTIC_MASK_STRATEGIES:
                msg = (
                    f"{side} mask column {col_name!r} uses strategy {strategy!r} which is "
                    "not deterministic; declared FK requires one of "
                    f"{sorted(DETERMINISTIC_MASK_STRATEGIES)} for cross-run join stability"
                )
                if strict_determinism:
                    result.add_error(
                        code=CODES.FK_NONDETERMINISTIC_MASK,
                        message=msg,
                        path=f"{path}.{side}.column",
                    )
                else:
                    # Advisory path: same code so platform validation routers
                    # (web/src/pipelines/hifi/validation.ts) can still pattern-
                    # match on it. Severity=warning is the only difference.
                    result.add_warning(
                        code=CODES.FK_NONDETERMINISTIC_MASK,
                        message=msg + " (advisory — set DECOY_FK_STRICT_DETERMINISM=1 to block)",
                        path=f"{path}.{side}.column",
                    )


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

    # ── FK preservation (Sprint 4, item 4) ──
    #
    # Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;
    # materialize parent pool; child samples with replacement.
    #
    # Read `column_relationships` from the parsed config. Each entry's
    # parent.node must stay live in the cache past its normal consumer
    # count so a downstream op can still query the pool. We extend
    # `keep_set` with every parent node referenced by an FK entry --
    # the cache's existing keep-set mechanism (cache.py) already
    # implements "do not evict at zero consumers."
    column_relationships = config.get("column_relationships") or []
    fk_parent_nodes: set[str] = {
        rel["parent"]["node"]
        for rel in column_relationships
        if isinstance(rel, dict) and isinstance(rel.get("parent"), dict)
        and "node" in rel["parent"]
    }

    keep_set = set(keep_nodes or []) | fk_parent_nodes
    cache = GraphCache(plan.consumer_counts, keep=keep_set)
    records: list[NodeRunRecord] = []
    overall_start = time.monotonic()
    success = True

    if ctx is None:
        ctx = ExecutionContext()

    # Bind `column_relationships` + `pool_resolver` onto the context.
    # `pool_resolver` closes over `cache` so it always sees live state
    # at the moment a child op asks; matches the existing derive_key
    # closure pattern (see ExecutionContext docstring).
    ctx.column_relationships = column_relationships if column_relationships else None
    if column_relationships:
        ctx.pool_resolver = _build_pool_resolver(cache, by_id)

    log = ctx.logger

    with _PeakRSSMonitor() as monitor:
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

            try:
                node_cfg = _resolve_node_exports(node_cfg, ctx._exports, nid)
            except _NodeExportResolutionError as exc:
                records.append(make_node_error_record(nid, kind, 0, str(exc)))
                if log is not None:
                    log.error("graph: node %s failed: %s", _node_descriptor(node), exc)
                success = False
                break

            in_edge_keys = plan.in_edges.get(nid, [])
            step_name = nid
            rows_in_total = cache.row_sum(in_edge_keys)
            inputs = [cache.consume(k, engine) for k in in_edge_keys]
            descriptor = _node_descriptor(node)

            emit_node_start(log, step_name, descriptor, engine, rows_in_total)

            if log is not None and node_cfg:
                log.info(_summarize_node_config(kind, node_cfg))

            t0 = time.monotonic()
            ctx._current_node_id = nid
            try:
                result = op.apply(inputs, node_cfg, ctx)
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
    """Best-effort sample of `node_id`'s output."""
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


def _build_pool_resolver(cache, by_id: dict[str, dict]):
    """Build the FK pool resolver closure for an ExecutionContext.

    Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;
    materialize parent pool; child samples with replacement.

    The closure is what generate_op.apply calls when it sees an FK
    declaration in `ctx.column_relationships`. The closed-over cache
    instance keeps the resolver live against cache state changes
    (further ops adding their output, parents getting pinned via the
    keep set built in _execute_graph). by_id is only used today for
    error messages; future versions may walk it to validate column
    presence at resolution time.

    Returns Callable[[parent_node_id, column], list[Any]]. Raises
    UnknownFKColumnError when the column is missing from the parent
    output. Raises EmptyParentPoolError when the parent output has
    zero distinct non-null values for the column. The graph errors
    translator (graph/errors.py::translate) maps both to stable
    validation_result.CODES values (fk.unknown_column /
    fk.empty_parent_pool).
    """
    from decoy_engine.exceptions import EmptyParentPoolError, UnknownFKColumnError

    def resolver(parent_node_id: str, column: str) -> list[Any]:
        table = cache.get(parent_node_id)
        if table is None:
            # Parent not yet materialized in the cache. Normally
            # unreachable because the topology validator (Commit 3)
            # rejects parent-after-child orderings; but if a graph
            # ran in lenient validation, fail clearly here.
            raise UnknownFKColumnError(
                f"parent node {parent_node_id!r} has no cached output yet "
                "(parent must run before child; check DAG topology)",
                parent_node=parent_node_id,
                parent_column=column,
            )
        try:
            column_array = table.column(column)
        except KeyError:
            raise UnknownFKColumnError(
                f"column {column!r} not present in parent {parent_node_id!r} "
                f"output (available columns: {table.schema.names})",
                parent_node=parent_node_id,
                parent_column=column,
            )
        # Drop nulls, then de-duplicate to a list in row order.
        # PyArrow's `drop_null` + `unique` is the canonical path; the
        # result is a ChunkedArray we convert to Python list once.
        try:
            import pyarrow.compute as pc
            distinct = pc.unique(pc.drop_null(column_array))
        except Exception:
            # If pyarrow.compute is unavailable, fall back to Python.
            raw = column_array.to_pylist()
            distinct = [v for v in dict.fromkeys(raw) if v is not None]
            if not distinct:
                raise EmptyParentPoolError(
                    f"parent {parent_node_id!r}.{column!r} has zero non-null values",
                    parent_node=parent_node_id,
                    parent_column=column,
                )
            return distinct
        values = distinct.to_pylist() if hasattr(distinct, "to_pylist") else list(distinct)
        if not values:
            raise EmptyParentPoolError(
                f"parent {parent_node_id!r}.{column!r} has zero non-null values",
                parent_node=parent_node_id,
                parent_column=column,
            )
        return values

    return resolver


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


_REDACT_KEYS = {"password", "secret", "token", "api_key", "apikey", "auth"}


def _summarize_node_config(kind: str, cfg: dict) -> str:
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


_NODE_TOKEN_RE = re.compile(r"\$\{nodes\.([a-zA-Z0-9_-]+)\.([a-zA-Z_][\w.]*)}")


class _NodeExportResolutionError(Exception):
    """Raised when a `${nodes.X.Y}` token can't be resolved."""


def _resolve_node_exports(
    cfg: Any,
    exports: dict[str, dict[str, Any]],
    current_node_id: str,
) -> Any:
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

"""Per-node execution loop and FK pool resolver.

Fourth (and final) sub-milestone of the runner decomposition
(V2.0-A.4 in the V2 sprint plan). Lifts the per-node execution loop
out of graph/runner.py so the runner becomes a thin facade that
wires validator + planner + memory monitor + executor together.

What lives here:
  - _execute_graph: builds the cache, iterates plan.order, calls each
    op's apply(), captures records, handles errors. The active
    execution path used by run_graph and execute_graph_capture.
  - _build_pool_resolver: factory for the FK pool resolver closure
    that generate_op consults when an FK declaration appears in
    ctx.column_relationships. Lives next to _execute_graph because
    they share the cache reference.

Pattern: pure-planning / impure-execution separation. The planner
(graph/planner.py) returns a frozen ExecutionPlan; this executor
consumes it. The executor's impurity (I/O via ops, mutation via
cache, side effects via emit_lineage) is intentional and isolated.

Why both functions sit in one module: the FK pool resolver closes
over the cache that _execute_graph constructs. Splitting them into
separate files would force passing the cache reference explicitly
and create a shape that misleads readers into thinking the pool
resolver is a standalone helper rather than part of one execution
loop's machinery.
"""

from __future__ import annotations

import time
from typing import Any

import pyarrow as pa

from decoy_engine.context import ExecutionContext, emit_lineage
from decoy_engine.exceptions import (
    ConfigError,
    EmptyParentPoolError,
    FlagPauseSignal,
    UnknownFKColumnError,
)
from decoy_engine.graph.errors import translate as translate_engine_error
from decoy_engine.graph.memory_monitor import PeakRSSMonitor, check_memory_pressure
from decoy_engine.graph.node_descriptors import (
    _node_descriptor,
    _summarize_node_config,
)
from decoy_engine.graph.node_exports import (
    _NodeExportResolutionError,
    _resolve_node_exports,
)
from decoy_engine.graph.run_state import RunState
from decoy_engine.graph.types import RunResult


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

    # Pin every FK parent node in the cache so it survives past its last
    # regular consumer.  Without this the parent would be evicted before
    # a downstream child op reads it as a pool.  The cache keep-set
    # mechanism (cache.py) implements "do not evict at zero consumers."
    column_relationships = config.get("column_relationships") or []
    fk_parent_nodes: set[str] = {
        rel["parent"]["node"]
        for rel in column_relationships
        if isinstance(rel, dict) and isinstance(rel.get("parent"), dict) and "node" in rel["parent"]
    }

    keep_set = set(keep_nodes or []) | fk_parent_nodes
    cache = GraphCache(plan.consumer_counts, keep=keep_set)

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

    # V2.0-A.1: per-execution state lives in a named dataclass. Earlier
    # versions of this function threaded `records`, `overall_start`,
    # `success`, and the current-node marker through as floating locals
    # and on `ctx._current_node_id`. The RunState aggregator makes those
    # writes explicit and greppable; the begin_node/end_node helpers
    # mirror current_node_id to ctx so the existing ctx.export() API
    # (used by every op) continues to route values to the right node.
    state = RunState(overall_start=time.monotonic())

    with PeakRSSMonitor() as monitor:
        state.memory_monitor = monitor
        # Emit lineage entries for every node before execution starts so
        # the audit trail is complete even if a node fails mid-run.
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
                state.records.append(make_node_error_record(nid, kind, 0, str(exc)))
                if log is not None:
                    log.error("graph: node %s failed: %s", _node_descriptor(node), exc)
                state.success = False
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
            state.begin_node(nid, ctx)
            try:
                # Defensive: cache.consume() returns None when the source
                # key isn't in the cache (skipped router port, gated branch,
                # or upstream that returned no table). Without this guard
                # the op fails with a native-engine error like DuckDB's
                # "Python Object 'df' of type 'NoneType' not suitable for
                # replacement scans" -- accurate but unhelpful to the
                # operator. Surface a runner-level error that names the
                # upstream edge(s) instead.
                none_edges = [k for k, v in zip(in_edge_keys, inputs, strict=False) if v is None]
                if none_edges:
                    raise ConfigError(
                        f"node {nid!r} ({kind}) has no data on input edge(s) "
                        f"{none_edges!r}: upstream node(s) produced no output. "
                        f"Check that the upstream node ran successfully and "
                        f"that any router/gate branch upstream routes data "
                        f"into this path."
                    )
                op_result = op.apply(inputs, node_cfg, ctx)  # type: ignore[attr-defined]
                if isinstance(op_result, dict) and getattr(op, "OUTPUT_KIND", None) == "split":
                    ports = getattr(op, "OUTPUT_PORTS", ())
                    total_rows = cache.write_split(nid, op_result, ports, engine)
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    state.records.append(
                        make_node_ok_record(
                            nid,
                            kind,
                            total_rows,
                            elapsed_ms,
                            ctx._exports.get(nid),
                        )
                    )
                    emit_node_ok(
                        log,
                        step_name,
                        descriptor,
                        rows_in_total,
                        total_rows,
                        elapsed_ms,
                        is_split=True,
                    )
                else:
                    rows_out = cache.write_stream(nid, op_result, engine)
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    state.records.append(
                        make_node_ok_record(
                            nid,
                            kind,
                            rows_out,
                            elapsed_ms,
                            ctx._exports.get(nid),
                        )
                    )
                    emit_node_ok(
                        log,
                        step_name,
                        descriptor,
                        rows_in_total,
                        rows_out,
                        elapsed_ms,
                    )
            except FlagPauseSignal:
                raise
            except Exception as exc:
                translated = translate_engine_error(exc, kind, nid)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                state.records.append(
                    make_node_error_record(
                        nid,
                        kind,
                        elapsed_ms,
                        str(translated),
                        exports=ctx._exports.get(nid),
                        error_code=getattr(translated, "code", None),
                        error_path=getattr(translated, "path", None),
                    )
                )
                emit_node_error(
                    log,
                    step_name,
                    descriptor,
                    rows_in_total,
                    exc,
                    translated,
                    nid,
                    elapsed_ms,
                )
                state.success = False
                break
            finally:
                state.end_node(ctx)

    check_memory_pressure(monitor.peak_rss, graph_engine_mode, log)

    result: RunResult = {
        "nodes": state.records,
        "success": state.success,
        "elapsed_ms": int((time.monotonic() - state.overall_start) * 1000),
    }
    return result, cache.kept()


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

    def resolver(parent_node_id: str, column: str) -> list[Any]:
        table = cache.get(parent_node_id)
        if table is None:
            # Parent not yet materialized in the cache. Normally
            # unreachable because the topology stage rejects
            # parent-after-child orderings; but if a graph ran in
            # lenient validation, fail clearly here.
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

            distinct = pc.unique(pc.drop_null(column_array))  # type: ignore[attr-defined]
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

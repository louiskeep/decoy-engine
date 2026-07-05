"""Option 2 (FK-RI memory-scaling): sequential load+mask+evict execution.

Masks an FK-related job one table at a time in FK-topological order instead of
holding every table full-width at once (the full-frame `PandasExecutionAdapter.run`
path). Each parent's narrow source->masked key map is built and retained BEFORE its
wide frame is evicted, so children still resolve against it and all orphan policies
keep working (unlike the streaming Option 1). On SUCCESS, output is byte-identical
to `run` (tests/unit/execution/test_sequential_eviction.py); the win is peak
memory, one table plus retained narrow key maps rather than every table full-width
plus all outputs.

Sink contract.

Plain Callable sink (back-compat, non-transactional): with a
``sink: Callable[[str, pa.Table], None]``, tables are emitted incrementally
in FK-topological order, so an abort partway through (an orphan ``FAIL``, or a
per-table guard rejection on a later table) leaves the tables emitted so far
already delivered to the sink. ``run`` is atomic (it raises before returning
any output); ``run_sequential(sink=<callable>)`` is not. A durable consumer
MUST treat an exception as "discard everything emitted for this run."

TransactionalSink: with a ``sink`` that satisfies
``execution._transactional_sink.TransactionalSink`` (has write/commit/abort),
``run_sequential`` commits on success and aborts on any exception, so the sink
sees all tables or none. On success, commit() is called once; on any exception,
abort() is called as a best-effort cleanup (abort errors are suppressed so the
original exception always propagates). This is the safe path for job-runner
wiring. See ``execution/_transactional_sink.py`` and ``ParquetTransactionalSink``
for the reference file-based implementation, which publishes via a single atomic
directory rename: a commit-time failure leaves the target untouched, and if the
target already exists non-empty, commit fails closed with nothing published.

Lives in its own module so `_pandas_adapter.py` stays under the orchestration LOC
cap. It reuses the adapter's per-node masking (`_dispatch_mask_node`) and parent-map
builder (`_parent_map`), so masking stays defined in one place.

Row-error enforcement (S2, engine "Finish Open-Ended Surfaces" program):
`run_sequential` owns the SAME D8 fail-loud/quarantine rule as `run()` for the
WHOLE sequential path (both sink and no-sink), because the routing added in
`_pipeline.py` makes this the default FK mask path reachable from the public
entry point. Per table, immediately after that table's mask-node loop:
drain + clear `ctx.row_errors` (fixing unbounded accumulation across tables),
then classify: any record not covered by an enabled quarantine trigger raises
`RowErrorsFailedError` BEFORE the parent key-map is built, BEFORE the table is
written to the sink, and BEFORE its frame is evicted -- so a failing table
never stages or commits a leaked value (the exception propagates to the
existing abort() handler, which discards any transactional sink staging).
Covered records are quarantine-filtered out of that table's Arrow output
(via `compute_quarantine`) before write/collect. Because the routing
predicate excludes job-level validators from this path, there are no
validator findings to reconcile here; only per-row `format_error` /
`mask_error` records are handled. The single quarantine JSONL is written once
after the loop (across all tables), not per-table, to avoid the truncating
`_write_jsonl("w")` clobbering earlier tables' entries.

Quarantine-aware FK resolution (S2 remediation, EXCLUDE-then-CASCADE): a
row-errored parent-key row must never leak its raw value through a child FK,
even though the parent's key-map is built from the FULL pre-filter frame. This
module folds each table's drained row-error records into a `key_error_rows`
index (table -> column -> {row_index: trigger}) and threads it, plus a
matching `errored_keys_cache`, through `adapter._parent_map` (excludes a
row-errored parent-key row from the map) and `adapter._dispatch_mask_node`
(so a child's `_resolve_fk_node` reads the excluded-key cache and cascades a
synthetic `RowError` onto the affected child rows). The parent table iterates
before its children (FK-topo order), so its `key_error_rows`/`errored_keys_cache`
entries are populated -- and its own uncovered-record raise has already
short-circuited if unhealthy -- before any child dispatch reads them. The
cascaded child `RowError`s drain on the CHILD's own per-table drain (same loop
iteration semantics as this module's D8 fail-loud/quarantine block above), so
they are classified and quarantine-filtered exactly like any other row error,
with no separate code path. See docs/backlog/s2-fk-leak-remediation-guide.md.

Design: docs/relationships-memory-scaling.md, sections 4 and 6.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._adapter import ExecutionResult, StrategyContext
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._guards import reject_null_bearing_int
from decoy_engine.execution._row_errors import RowErrorRecord, drain_row_errors
from decoy_engine.execution._runner import WorkNode, build_work_list, order_work
from decoy_engine.execution._transactional_sink import (
    TransactionalSink,
    _CallableSinkAdapter,
)
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.instrumentation.timing import TimingCollector, use_collector
from decoy_engine.quarantine import _write_jsonl, compute_quarantine, quarantine_manifest
from decoy_engine.validators._types import QuarantineSummary

if TYPE_CHECKING:
    from collections.abc import Callable

    import pandas as pd

    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph

_NodeKey = tuple[str, tuple[str, ...]]
_KeyTuple = tuple[object, ...]


def run_sequential(
    adapter: PandasExecutionAdapter,
    plan: Plan,
    source_loader: Callable[[str], pa.Table],
    *,
    registry: ProviderRegistry,
    pool_cache: PoolCache | None = None,
    relationship_graph: RelationshipGraph,
    namespace_registry: NamespaceRegistry,
    sink: TransactionalSink | Callable[[str, pa.Table], None] | None = None,
    quarantine_config: dict[str, Any] | None = None,
) -> ExecutionResult:
    """Mask an FK-related job table by table in FK-topological order.

    `source_loader(table)` is invoked once per table in the plan/graph table set
    (the tables named in `plan.seed_envelope.per_table` plus any table in a graph
    edge), and must return that table's Arrow source. Unlike `run`, which emits
    exactly the tables in its `sources` mapping, this path is driven by the plan:
    a table present only in the caller's sources but absent from the plan and graph
    is neither loaded nor emitted.

    With `sink`, each masked table is emitted then dropped (outputs not
    accumulated, so `ExecutionResult.outputs` is empty); without `sink`, outputs
    are collected like `run`.

    If `sink` satisfies `TransactionalSink` (has write/commit/abort), the run is
    transactional: commit() is called on success; abort() is called on any
    exception as best-effort cleanup (abort errors are swallowed so the original
    exception always propagates). For ParquetTransactionalSink specifically,
    commit is a single atomic directory rename: a commit-time failure publishes
    nothing, and if the target already exists non-empty, commit fails closed.
    A plain Callable sink is wrapped in a no-op adapter that preserves the
    pre-existing non-transactional contract (partial output on abort is documented
    and pinned by test).

    `quarantine_config` (S2, default None) enforces the SAME per-row D8 rule
    `run()` enforces via `run_pipeline`: any per-row `format_error` / `mask_error`
    NOT covered by an enabled quarantine trigger raises `RowErrorsFailedError`
    for that table before it is written or evicted (fail-closed by default,
    matching `run()`'s honesty guarantee). Covered records are filtered out of
    that table's output before write. `ExecutionResult.row_errors` carries every
    drained record (covered or not) for caller-side reporting.
    """
    q_cfg: dict[str, Any] = quarantine_config or {}
    q_enabled = bool(q_cfg.get("enabled", False))
    q_triggers: list[str] = list(q_cfg.get("triggers") or [])
    q_output_path: str = (q_cfg.get("output_path") or "").strip()
    # Fail-closed backstop for raw-dict callers who bypass QuarantineConfig
    # validation: if quarantine is enabled with a row-error trigger, it must name
    # an output_path, or a quarantined row would be silently dropped.
    if q_enabled and q_triggers and not q_output_path:
        raise ValueError(
            "quarantine enabled with triggers but no output_path; refusing to run "
            "(would silently drop quarantined rows)."
        )

    all_row_errors: list[RowErrorRecord] = []
    quarantine_entries: list[dict[str, Any]] = []
    counts_by_trigger: dict[str, int] = {}

    graph = relationship_graph
    ordered = order_work(build_work_list(plan, registry), graph)
    node_by_key: dict[_NodeKey, WorkNode] = {n.key: n for n in ordered}
    nodes_by_table: dict[str, list[WorkNode]] = {}
    for node in ordered:
        nodes_by_table.setdefault(node.table, []).append(node)

    table_order = table_topo_order(plan, graph)

    # A parent key map is retained until every child table that references it has
    # been processed; this makes multi-parent and diamond graphs safe.
    remaining_child_consumers: dict[_NodeKey, set[str]] = {}
    for edge in graph.edges:
        ck = (edge.parent_table, edge.parent_columns)
        remaining_child_consumers.setdefault(ck, set()).add(edge.child_table)

    cache = pool_cache if pool_cache is not None else PoolCache()
    ctx = StrategyContext(
        registry=registry,
        pool_cache=cache,
        relationship_graph=graph,
        namespace_registry=namespace_registry,
        job_seed=plan.seed_envelope.job_seed,
    )

    parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]] = {}
    # S2 (quarantine-aware FK resolution): errored_keys_cache mirrors
    # parent_map_cache (same cache_key); key_error_rows is the incrementally
    # folded per-table/per-column row-error index that feeds both.
    errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]] = {}
    key_error_rows: dict[str, dict[str, dict[int, str]]] = {}
    source_snapshots: dict[tuple[str, str], pd.Series] = {}
    frames: dict[str, pd.DataFrame] = {}
    outputs: dict[str, pa.Table] = {}
    warnings: list[QualityWarning] = []
    conversion_ms = 0.0
    collector = TimingCollector()

    # Resolve which sink protocol to use once, before the loop.
    # isinstance with a runtime_checkable Protocol checks for write/commit/abort.
    _tsink: TransactionalSink | None
    if isinstance(sink, TransactionalSink):
        _tsink = sink
    elif sink is not None:
        _tsink = _CallableSinkAdapter(sink)
    else:
        _tsink = None

    try:
        with use_collector(collector):
            for table in table_order:
                src = source_loader(table)
                # Same guard as run(), table-local (FK children exempt via graph).
                reject_null_bearing_int(plan, {table: src}, registry, graph)
                t0 = time.perf_counter()
                df = src.to_pandas()
                conversion_ms += (time.perf_counter() - t0) * 1000.0
                frames[table] = df
                del src

                # Snapshot this table's parent-key columns pre-mask, for its outgoing
                # edges, so a child can rebuild the key map after eviction.
                for edge in graph.edges:
                    if edge.parent_table == table:
                        for col in edge.parent_columns:
                            if col in df.columns and (table, col) not in source_snapshots:
                                source_snapshots[(table, col)] = df[col].copy()

                for node in nodes_by_table.get(table, ()):
                    warnings.extend(
                        adapter._dispatch_mask_node(
                            node,
                            frames,
                            graph,
                            source_snapshots,
                            parent_map_cache,
                            node_by_key,
                            ctx,
                            key_error_rows=key_error_rows,
                            errored_keys_cache=errored_keys_cache,
                        )
                    )

                # S2 D8: drain + clear ctx.row_errors for THIS table right after
                # its mask-node loop (fixes unbounded cross-table accumulation),
                # then fail loud on anything not covered by an enabled quarantine
                # trigger, BEFORE the parent map is built, BEFORE this table is
                # written to the sink, and BEFORE its frame is evicted. A
                # failing table therefore never stages or commits a leaked
                # value: the raise below propagates to the except clause,
                # which aborts the transactional sink (discarding any tables
                # already staged) before re-raising.
                table_records = drain_row_errors(ctx.row_errors, table=table)
                all_row_errors.extend(table_records)
                # S2: fold this table's records into the key-error index BEFORE
                # the fail-loud classification, so an uncovered raise below still
                # leaves the (correct, excluded) index state -- though the raise
                # short-circuits before any child of THIS table is ever dispatched.
                for rec in table_records:
                    key_error_rows.setdefault(rec.table, {}).setdefault(rec.column, {})[
                        rec.row_index
                    ] = rec.trigger
                if table_records:
                    uncovered = tuple(
                        r for r in table_records if not (q_enabled and r.trigger in q_triggers)
                    )
                    if uncovered:
                        raise RowErrorsFailedError(uncovered)

                # Build + cache outgoing parent maps now, before evicting the frame.
                # This reads the FULL pre-filter frame, so children resolve
                # against the complete key map exactly as in full-frame `run()`;
                # quarantine-filtering the OUTPUT below never perturbs FK
                # resolution (byte-parity, see module docstring). S2: threading
                # key_error_rows/errored_keys_cache here excludes any row-errored
                # parent-key row from the map and records it for child cascade.
                for edge in graph.edges:
                    if edge.parent_table == table:
                        adapter._parent_map(
                            edge,
                            frames,
                            source_snapshots,
                            parent_map_cache,
                            key_error_rows=key_error_rows,
                            errored_keys_cache=errored_keys_cache,
                        )

                t1 = time.perf_counter()
                out = pa.Table.from_pandas(frames[table], preserve_index=False)
                conversion_ms += (time.perf_counter() - t1) * 1000.0

                if table_records:  # reaching here means every record was covered
                    filtered, entries, counts, _total = compute_quarantine(
                        {table: out}, None, q_cfg, row_errors=table_records
                    )
                    out = filtered[table]
                    quarantine_entries.extend(entries)
                    for trig, n in counts.items():
                        counts_by_trigger[trig] = counts_by_trigger.get(trig, 0) + n

                if _tsink is not None:
                    _tsink.write(table, out)
                else:
                    outputs[table] = out

                # Evict this table's wide frame + its pre-mask snapshots (the narrow
                # maps it produced stay cached for downstream children).
                del frames[table]
                for snap_key in [k for k in source_snapshots if k[0] == table]:
                    del source_snapshots[snap_key]

                # Release any parent map whose every child consumer is now done.
                for edge in graph.edges:
                    if edge.child_table == table:
                        ck = (edge.parent_table, edge.parent_columns)
                        consumers = remaining_child_consumers.get(ck)
                        if consumers is not None:
                            consumers.discard(table)
                            if not consumers:
                                parent_map_cache.pop(ck, None)
                                errored_keys_cache.pop(ck, None)

        # Finalize row-error / quarantine evidence AFTER every table has masked
        # and BEFORE commit, still inside this try so a failure here also
        # triggers abort() rather than a partial, uncommitted publish. A
        # single JSONL write covers every table's quarantined rows (avoiding
        # the truncating `_write_jsonl("w")` clobbering earlier tables').
        # Quarantine JSONL is durable only on a successful (fully covered) run;
        # a fail-loud run publishes nothing.
        quality_metrics: dict[str, Any] = {}
        if all_row_errors:
            row_error_counts: dict[str, int] = {}
            for rec in all_row_errors:
                key = f"{rec.table}.{rec.column}[{rec.trigger}]"
                row_error_counts[key] = row_error_counts.get(key, 0) + 1
            quality_metrics["row_errors"] = row_error_counts
        if quarantine_entries:
            _write_jsonl(q_output_path, quarantine_entries)
            # total_quarantined = distinct rows removed (each entry is one
            # distinct quarantined row; dedup happens per table inside
            # compute_quarantine, and row indices are per-table).
            quality_metrics["quarantine"] = quarantine_manifest(
                QuarantineSummary(
                    enabled=True,
                    output_path=q_output_path,
                    counts_by_trigger=counts_by_trigger,
                    total_quarantined=len(quarantine_entries),
                )
            )

        if _tsink is not None:
            _tsink.commit()

    except BaseException:
        if _tsink is not None:
            try:
                _tsink.abort()
            except Exception:
                # Abort is best-effort; swallow cleanup errors so the original
                # exception propagates unmasked via the bare raise below.
                pass
        raise

    return ExecutionResult(
        outputs=outputs,
        timings=tuple(collector.records),
        boundary_conversion_ms=conversion_ms,
        warnings=tuple(warnings),
        quality_metrics=quality_metrics,
        row_errors=tuple(all_row_errors),
    )


def table_topo_order(plan: Plan, graph: RelationshipGraph) -> list[str]:
    """FK-topological order over TABLES (every parent before its children), stable
    to the plan's table order where the graph leaves a choice. Raises on a cycle.
    Sequential eviction relies on a parent being fully masked, and its key map
    built, before any child table is loaded."""
    order_seed: list[str] = []
    seen: set[str] = set()
    for name, _ in plan.seed_envelope.per_table:
        if name not in seen:
            seen.add(name)
            order_seed.append(name)
    for edge in graph.edges:
        for tbl in (edge.parent_table, edge.child_table):
            if tbl not in seen:
                seen.add(tbl)
                order_seed.append(tbl)

    position = {t: i for i, t in enumerate(order_seed)}
    children: dict[str, set[str]] = {t: set() for t in order_seed}
    indegree: dict[str, int] = dict.fromkeys(order_seed, 0)
    for edge in graph.edges:
        if edge.parent_table == edge.child_table:
            continue  # self-FK masks within one table; no table-level ordering
        if edge.child_table not in children[edge.parent_table]:
            children[edge.parent_table].add(edge.child_table)
            indegree[edge.child_table] += 1

    ready = [t for t in order_seed if indegree[t] == 0]
    result: list[str] = []
    while ready:
        ready.sort(key=position.__getitem__)
        current = ready.pop(0)
        result.append(current)
        for child in sorted(children[current], key=position.__getitem__):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)

    if len(result) != len(order_seed):
        raise ExecutionError(
            code="relationship_cycle",
            message=(
                "FK relationship graph has a cycle across tables; cannot order "
                "tables for sequential masking."
            ),
        )
    return result

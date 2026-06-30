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

Design: docs/relationships-memory-scaling.md, sections 4 and 6.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult, StrategyContext
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._guards import reject_null_bearing_int
from decoy_engine.execution._runner import WorkNode, build_work_list, order_work
from decoy_engine.execution._transactional_sink import (
    TransactionalSink,
    _CallableSinkAdapter,
)
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.instrumentation.timing import TimingCollector, use_collector

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
    """
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
                        )
                    )

                # Build + cache outgoing parent maps now, before evicting the frame.
                for edge in graph.edges:
                    if edge.parent_table == table:
                        adapter._parent_map(edge, frames, source_snapshots, parent_map_cache)

                t1 = time.perf_counter()
                out = pa.Table.from_pandas(frames[table], preserve_index=False)
                conversion_ms += (time.perf_counter() - t1) * 1000.0
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
        quality_metrics={},
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

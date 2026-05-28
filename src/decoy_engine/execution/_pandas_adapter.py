"""PandasExecutionAdapter: the first concrete ExecutionAdapter (engine-v2 S9).

Arrow-shaped boundary (`pa.Table` in/out) with a single conversion site each way
PER TABLE (S9 spec §3 + §7). The boundary is MULTI-TABLE (slice 2h / PQ-S9-C):
`run(sources: Mapping[str, pa.Table])` masks an FK parent and its child in one
call so the child's FK columns resolve against the parent's in-run source->masked
map. `run_single` is the single-table convenience wrapper (no FK data to thread).

The run loop:

1. Convert each source `pa.Table` -> `pd.DataFrame` (timed).
2. Build the work list from `plan.seed_envelope` (NOT FK-only `plan.ordering`).
3. Order it (FK parents before children, including composite-PK parent columns
   before a composite-FK child; plus R17 composite-before-child).
4. Dispatch each node in dependency order: a node that is an FK CHILD resolves
   through the parent source->masked map + the edge's OrphanPolicy; every other
   node masks via its scalar/composite handler. FK-parent columns are snapshotted
   up front (pre-mask) so a child can reconstruct the parent key mapping.
5. Convert each frame back to `pa.Table` and return `ExecutionResult`.

Dispatch is serial. Runner-level per-column (Faker) parallelism (spec 5.1) is
deferred to S13: the S4 faker adapter shares a per-locale Faker instance and does
seed_instance()+generate, so concurrent pool builds for one locale are not
thread-safe (they race on the shared RNG and break determinism). Making the
adapter thread-safe is an S4 change, and the >=10x Faker Performance Gate it feeds
is S13's. FPE's per-row chunked parallelism is independent (pure derive per row)
and stays live via the `fpe_chunk_count` knob.
"""

from __future__ import annotations

import numbers
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._adapter import (
    ExecutionAdapter,
    ExecutionResult,
    StrategyContext,
    StrategyHandler,
)
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import WorkNode, build_work_list, order_work
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution._strategies._composite import CompositeHandler
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution._strategies._orphan import resolve_fk_keys
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.instrumentation.timing import TimingCollector, timed_strategy, use_collector
from decoy_engine.plan._types import ColumnSeed

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph
    from decoy_engine.relationships._graph import RelationshipEdge

_NodeKey = tuple[str, tuple[str, ...]]
_KeyTuple = tuple[object, ...]


def _fk_key_value(value: object) -> object:
    """Normalize one FK key component so equal logical keys match across the
    int/float dtype split pandas introduces (an int64 parent column vs a
    float64-because-null child column read by `to_pandas()`). Numpy integers and
    whole-number floats collapse to a Python int; everything else passes through
    (Dennis slice-2h F2). Nulls never reach here -- they are filtered upstream."""
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    return value


class PandasExecutionAdapter:
    """Concrete pandas-backed execution adapter."""

    adapter_name: str = "pandas"
    adapter_version: str = pd.__version__

    def __init__(self, *, fpe_chunk_count: int = 4) -> None:
        self._fpe_chunk_count = fpe_chunk_count
        self._composite_handler = CompositeHandler()
        # Per-adapter handler table so the `fpe_chunk_count` knob is live: the
        # FPE handler is reconstructed with the configured chunk count (the
        # module-level SCALAR_HANDLERS uses the handler default). All other
        # handlers are stateless and shared.
        self._handlers: dict[str, StrategyHandler] = dict(SCALAR_HANDLERS)
        self._handlers["fpe"] = FpeStrategyHandler(chunk_count=fpe_chunk_count)

    def supports_strategy(self, strategy_name: str) -> bool:
        return strategy_name in self._handlers

    def shutdown(self) -> None:
        """Idempotent resource release. No long-lived pools held; safe any time."""
        return None

    def run_single(
        self,
        plan: Plan,
        source: pa.Table,
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
        table: str | None = None,
    ) -> ExecutionResult:
        """Single-table convenience over `run`. Infers the table name from the
        plan when it has exactly one table; pass `table` explicitly otherwise."""
        if table is None:
            names = [name for name, _ in plan.seed_envelope.per_table]
            if len(names) != 1:
                raise ExecutionError(
                    code="run_single_requires_table",
                    message=(
                        f"run_single needs an explicit table= for a {len(names)}-table "
                        "plan; use run(sources=...) for multi-table jobs."
                    ),
                )
            table = names[0]
        return self.run(
            plan,
            {table: source},
            registry=registry,
            pool_cache=pool_cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
        )

    def run(
        self,
        plan: Plan,
        sources: Mapping[str, pa.Table],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
    ) -> ExecutionResult:
        t0 = time.perf_counter()
        frames: dict[str, pd.DataFrame] = {t: tbl.to_pandas() for t, tbl in sources.items()}
        conversion_ms = (time.perf_counter() - t0) * 1000.0

        cache = pool_cache if pool_cache is not None else PoolCache()
        ctx = StrategyContext(
            registry=registry,
            pool_cache=cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
            job_seed=plan.seed_envelope.job_seed,
        )

        ordered = order_work(build_work_list(plan, registry), relationship_graph)
        node_by_key: dict[_NodeKey, WorkNode] = {n.key: n for n in ordered}

        # FK-parent columns are snapshotted pre-mask so an FK child can rebuild
        # the parent source->masked key map. Parents always mask before children
        # (parallel scalars run first, then composites; FK children resolve last),
        # so an up-front snapshot of every parent column is pre-mask by construction.
        parent_cols: dict[str, set[str]] = {}
        for edge in relationship_graph.edges:
            parent_cols.setdefault(edge.parent_table, set()).update(edge.parent_columns)
        source_snapshots: dict[tuple[str, str], pd.Series] = {
            (table, col): frames[table][col].copy()
            for table in frames
            for col in parent_cols.get(table, set())
            if col in frames[table].columns
        }
        parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]] = {}

        # Serial dispatch in dependency order: FK parents (scalar/composite) mask
        # before FK children resolve. Runner-level per-column (Faker) parallelism
        # (spec 5.1) is DEFERRED to S13: the S4 faker adapter shares a per-locale
        # Faker instance and does seed_instance()+generate, so concurrent pool
        # builds for the same locale race and break determinism. Making that
        # thread-safe is an S4 change; its >=10x Performance Gate lives in S13.
        # FPE's per-value chunked parallelism is independent of this (pure per-row
        # derive) and stays live via fpe_chunk_count.
        warnings: list[QualityWarning] = []
        collector = TimingCollector()
        with use_collector(collector):
            for node in ordered:
                if node.table not in frames:
                    continue
                df = frames[node.table]
                child_edges = relationship_graph.parents_of(node.table, node.columns)
                if child_edges:
                    with timed_strategy("fk_resolve", ",".join(node.columns)):
                        node_warnings = self._resolve_fk_node(
                            node,
                            child_edges[0],
                            frames,
                            source_snapshots,
                            parent_map_cache,
                            node_by_key,
                            ctx,
                        )
                    warnings.extend(node_warnings)
                    continue
                if node.kind == "composite":
                    with timed_strategy("composite", ",".join(node.columns)):
                        frames[node.table], node_warnings = self._composite_handler.run(
                            df, node, ctx
                        )
                    warnings.extend(node_warnings)
                    continue
                if node.kind != "scalar":
                    raise ExecutionError(
                        code="composite_fk_group_no_edge",
                        message=(
                            f"node kind {node.kind!r} (columns={node.columns}) on table "
                            f"{node.table!r} is not an FK child but is not a scalar/composite "
                            "node; the relationship graph has no edge for it."
                        ),
                    )
                handler = self._handlers.get(node.strategy)
                if handler is None:
                    raise ExecutionError(
                        code="unsupported_strategy",
                        message=f"no handler for strategy {node.strategy!r} on {node.columns}.",
                    )
                plan_slice = node.plan_slice
                if not isinstance(plan_slice, ColumnSeed):  # narrows for the scalar handler
                    raise ExecutionError(
                        code="unsupported_strategy",
                        message=f"scalar node {node.columns} has a non-ColumnSeed plan slice.",
                    )
                with timed_strategy(node.strategy, ",".join(node.columns)):
                    frames[node.table], node_warnings = handler.run(
                        df, node.columns[0], plan_slice, ctx
                    )
                warnings.extend(node_warnings)

        t1 = time.perf_counter()
        outputs = {t: pa.Table.from_pandas(f, preserve_index=False) for t, f in frames.items()}
        conversion_ms += (time.perf_counter() - t1) * 1000.0

        return ExecutionResult(
            outputs=outputs,
            timings=tuple(collector.records),
            boundary_conversion_ms=conversion_ms,
            warnings=tuple(warnings),
            quality_metrics={},
        )

    def _resolve_fk_node(
        self,
        node: WorkNode,
        edge: RelationshipEdge,
        frames: dict[str, pd.DataFrame],
        source_snapshots: dict[tuple[str, str], pd.Series],
        parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]],
        node_by_key: dict[_NodeKey, WorkNode],
        ctx: StrategyContext,
    ) -> list[QualityWarning]:
        """Mask an FK child node by mapping its source key through the parent
        source->masked map and applying the edge's OrphanPolicy. Serves both a
        scalar FK child (1-tuple keys) and a composite-FK group (N-tuple keys)."""
        parent_map = self._parent_map(edge, frames, source_snapshots, parent_map_cache)
        child_frame = frames[node.table]
        child_cols = edge.child_columns
        n = len(child_frame)

        col_vals = [child_frame[c] for c in child_cols]
        col_na = [child_frame[c].isna() for c in child_cols]
        child_keys: list[_KeyTuple | None] = []
        for i in range(n):
            if any(bool(na.iloc[i]) for na in col_na):
                child_keys.append(None)  # null FK: preserved, never an orphan
            else:
                child_keys.append(tuple(_fk_key_value(v.iloc[i]) for v in col_vals))

        remap_fn = self._make_remap_fn(edge, node_by_key, ctx)
        masked_keys, warnings = resolve_fk_keys(child_keys, parent_map, edge, remap_fn=remap_fn)

        for j, c in enumerate(child_cols):
            child_frame[c] = [None if mk is None else mk[j] for mk in masked_keys]
        return warnings

    def _parent_map(
        self,
        edge: RelationshipEdge,
        frames: dict[str, pd.DataFrame],
        source_snapshots: dict[tuple[str, str], pd.Series],
        parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]],
    ) -> dict[_KeyTuple, _KeyTuple]:
        """Build (cached) the parent source-key -> masked-key map for an edge.

        Source values come from the pre-mask snapshot taken when the parent
        column masked; masked values from the now-mutated parent frame. A parent
        column never masked (no snapshot) maps identity (source == current), which
        is the correct RI behavior for an unmasked parent key.
        """
        cache_key: _NodeKey = (edge.parent_table, edge.parent_columns)
        cached = parent_map_cache.get(cache_key)
        if cached is not None:
            return cached
        ptable = edge.parent_table
        if ptable not in frames:
            parent_map_cache[cache_key] = {}
            return {}
        masked_frame = frames[ptable]
        pcols = edge.parent_columns
        src_series = [source_snapshots.get((ptable, c), masked_frame[c]) for c in pcols]
        masked_series = [masked_frame[c] for c in pcols]
        n = len(masked_frame)
        out: dict[_KeyTuple, _KeyTuple] = {}
        for i in range(n):
            raw = [s.iloc[i] for s in src_series]
            if any(pd.isna(x) for x in raw):
                continue  # parent key with a null component cannot be referenced
            src_t = tuple(_fk_key_value(x) for x in raw)
            out[src_t] = tuple(s.iloc[i] for s in masked_series)
        parent_map_cache[cache_key] = out
        return out

    def _make_remap_fn(
        self,
        edge: RelationshipEdge,
        node_by_key: dict[_NodeKey, WorkNode],
        ctx: StrategyContext,
    ) -> Callable[[list[_KeyTuple]], list[_KeyTuple]]:
        """A REMAP closure: mask orphan source keys via the PARENT columns' own
        strategies, so a remapped orphan is indistinguishable from a real masked
        value (S9 spec §6.2 REMAP + Dennis slice-2h brief §G)."""
        ptable = edge.parent_table
        pcols = edge.parent_columns

        def remap(orphan_keys: list[_KeyTuple]) -> list[_KeyTuple]:
            if not orphan_keys:
                return []
            masked_cols: list[list[object]] = []
            for j, pcol in enumerate(pcols):
                pnode = node_by_key.get((ptable, (pcol,)))
                if pnode is None or not isinstance(pnode.plan_slice, ColumnSeed):
                    raise ExecutionError(
                        code="orphan_remap_parent_missing",
                        message=(
                            f"REMAP needs the parent column {ptable}.{pcol} to be a "
                            "masked scalar node, but it is absent from the work list."
                        ),
                    )
                handler = self._handlers.get(pnode.strategy)
                if handler is None:
                    raise ExecutionError(
                        code="unsupported_strategy",
                        message=f"REMAP found no handler for parent strategy {pnode.strategy!r}.",
                    )
                tmp = pd.DataFrame({pcol: [k[j] for k in orphan_keys]})
                tmp, _ = handler.run(tmp, pcol, pnode.plan_slice, ctx)
                masked_cols.append(list(tmp[pcol]))
            return [tuple(col[i] for col in masked_cols) for i in range(len(orphan_keys))]

        return remap


_DEFAULT_EXECUTORS: dict[str, ExecutionAdapter] = {}


def get_default_executor() -> ExecutionAdapter:
    """Return the cached default execution adapter for the current substrate.

    S12 (M2): the engine reads its own DECOY_SUBSTRATE contract here and resolves
    the adapter via `select_execution_adapter`, so a caller (the platform job
    runner) routes a full job through the selected substrate by calling this; it
    does not re-implement substrate selection (best-practices section 3.3). One
    cached instance per substrate value (the singleton holds for a fixed env).
    """
    from decoy_engine.execution._substrate import resolve_substrate, select_execution_adapter

    substrate = resolve_substrate()
    cached = _DEFAULT_EXECUTORS.get(substrate)
    if cached is None:
        cached = select_execution_adapter()
        _DEFAULT_EXECUTORS[substrate] = cached
    return cached


def _reset_default_executor_for_tests() -> None:
    _DEFAULT_EXECUTORS.clear()

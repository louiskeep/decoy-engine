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
from decoy_engine.execution._fk_keys import (
    fk_columns_for_table,
    fk_key_value,
    lossless_fk_int_values,
    to_pandas_fk_safe,
)
from decoy_engine.execution._guards import reject_null_bearing_int
from decoy_engine.execution._row_errors import RowErrorRecord, drain_row_errors
from decoy_engine.execution._runner import WorkNode, build_work_list, order_work
from decoy_engine.execution._sequential import run_sequential as _run_sequential
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution._strategies._composite import CompositeHandler
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution._strategies._orphan import (
    cascade_row_errors,
    gather_errored_parent_keys,
    make_remap_fn,
    resolve_fk_keys,
)
from decoy_engine.execution._transactional_sink import TransactionalSink
from decoy_engine.execution._when_gate import run_with_when_gate
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
# S2 (quarantine-aware FK resolution): table -> column -> {row_index: trigger},
# folded incrementally as each table's row-errors are drained. Only KEY columns
# matter to `_parent_map`, but every drained record is folded (cheap; `_parent_map`
# intersects with `edge.parent_columns`).
_KeyErrorRows = dict[str, dict[str, dict[int, str]]]


def _fk_key_value(value: object) -> object:
    """Compatibility wrapper for tests and older internal imports.

    Delegates to `decoy_engine.execution._fk_keys.fk_key_value`, the single
    normalization shared by this adapter and the out-of-core route (SC1
    port), so equal logical keys match across the int/float dtype split
    pandas introduces (an int64 parent column vs a float64-because-null child
    column read by `to_pandas()`). Both call sites in this module filter
    nulls before calling this, so the null/NaN sentinel branch in
    `fk_key_value` is unreachable here -- a superset, not a divergence."""
    return fk_key_value(value)


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
        # B1 (S13): reject integer + null-bearing columns under truncate/hash/
        # categorical on the Arrow sources, before to_pandas widens int+null to
        # float. Backstops the plan-compile check for the no_profile path; both
        # adapters reject identically (no silent cross-substrate divergence). FK
        # children are exempt (resolved via the edge, not masked).
        reject_null_bearing_int(plan, sources, registry, relationship_graph)
        t0 = time.perf_counter()
        # DE-10: FK parent/child key columns route through the lossless-typing
        # contract (execution/_fk_keys.py) instead of a bare `to_pandas()`, so a
        # null-bearing integer FK column never silently widens to float64 and
        # rounds a key beyond 2**53. Every other column is unaffected.
        frames: dict[str, pd.DataFrame] = {
            t: to_pandas_fk_safe(tbl, fk_columns_for_table(relationship_graph.edges, t))
            for t, tbl in sources.items()
        }
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
        # S2: mirrors parent_map_cache with row-errored parent keys excluded.
        errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]] = {}
        key_error_rows: _KeyErrorRows = {}

        # Serial dispatch in dependency order: FK parents (scalar/composite) mask
        # before FK children resolve. Runner-level per-column (Faker) parallelism
        # (spec 5.1) is DEFERRED to S13: the S4 faker adapter shares a per-locale
        # Faker instance and does seed_instance()+generate, so concurrent pool
        # builds for the same locale race and break determinism. Making that
        # thread-safe is an S4 change; its >=10x Performance Gate lives in S13.
        # FPE's per-value chunked parallelism is independent of this (pure per-row
        # derive) and stays live via fpe_chunk_count.
        warnings: list[QualityWarning] = []
        row_error_records: list[RowErrorRecord] = []
        collector = TimingCollector()
        with use_collector(collector):
            for node in ordered:
                if node.table not in frames:
                    continue
                warnings.extend(
                    self._dispatch_mask_node(
                        node,
                        frames,
                        relationship_graph,
                        source_snapshots,
                        parent_map_cache,
                        node_by_key,
                        ctx,
                        key_error_rows=key_error_rows,
                        errored_keys_cache=errored_keys_cache,
                    )
                )
                # Sprint 2 honesty pack (D7): drain the shared row_errors sink
                # right after EVERY node dispatch (scalar, composite, and
                # fk_resolve all route through `_dispatch_mask_node`),
                # attributing to node.table, so no row error is ever silently
                # dropped regardless of which node produced it.
                batch = drain_row_errors(ctx.row_errors, table=node.table)
                row_error_records.extend(batch)
                # S2: fold into the key-error index; parents mask+drain before
                # children dispatch (order_work), so this is populated in time.
                for rec in batch:
                    key_error_rows.setdefault(rec.table, {}).setdefault(rec.column, {})[
                        rec.row_index
                    ] = rec.trigger

        t1 = time.perf_counter()
        outputs = {t: pa.Table.from_pandas(f, preserve_index=False) for t, f in frames.items()}
        conversion_ms += (time.perf_counter() - t1) * 1000.0

        return ExecutionResult(
            outputs=outputs,
            timings=tuple(collector.records),
            boundary_conversion_ms=conversion_ms,
            warnings=tuple(warnings),
            quality_metrics={},
            row_errors=tuple(row_error_records),
        )

    def _dispatch_mask_node(
        self,
        node: WorkNode,
        frames: dict[str, pd.DataFrame],
        relationship_graph: RelationshipGraph,
        source_snapshots: dict[tuple[str, str], pd.Series],
        parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]],
        node_by_key: dict[_NodeKey, WorkNode],
        ctx: StrategyContext,
        *,
        key_error_rows: _KeyErrorRows | None = None,
        errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]] | None = None,
    ) -> list[QualityWarning]:
        """Mask one work node in place in `frames[node.table]`, returning its
        warnings. Shared by the full-frame `run` and the sequential
        `run_sequential` so both paths mask byte-identically. An FK child resolves
        through the parent source->masked map + the edge's OrphanPolicy; every
        other node masks via its scalar or composite handler. `key_error_rows`/
        `errored_keys_cache` (S2, default None) thread the quarantine-aware FK
        caches to `_resolve_fk_node`; non-FK nodes ignore them."""
        df = frames[node.table]
        child_edges = relationship_graph.parents_of(node.table, node.columns)
        if child_edges:
            with timed_strategy("fk_resolve", ",".join(node.columns)):
                return self._resolve_fk_node(
                    node,
                    child_edges,
                    frames,
                    source_snapshots,
                    parent_map_cache,
                    node_by_key,
                    ctx,
                    key_error_rows=key_error_rows,
                    errored_keys_cache=errored_keys_cache,
                )
        if node.kind == "composite":
            with timed_strategy("composite", ",".join(node.columns)):
                frames[node.table], node_warnings = self._composite_handler.run(df, node, ctx)
            return node_warnings
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
            # MG-3 / M3 (2026-05-31): the when_gate is a no-op when
            # plan_slice.when is None (byte-identical to calling handler.run()
            # directly). When set, it evaluates the predicate via numexpr
            # (scope-clamped per Dennis C1) and routes only matching rows through.
            frames[node.table], node_warnings = run_with_when_gate(
                handler, df, node.columns[0], plan_slice, ctx
            )
        return node_warnings

    def run_sequential(
        self,
        plan: Plan,
        source_loader: Callable[[str], pa.Table],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
        sink: TransactionalSink | Callable[[str, pa.Table], None] | None = None,
        quarantine_config: dict[str, object] | None = None,
    ) -> ExecutionResult:
        """Option 2 (FK-RI memory-scaling): mask an FK-related job one table at a
        time in FK-topological order, evicting each table's wide frame after its
        narrow source->masked key map is built, so children still resolve and all
        orphan policies keep working. `source_loader(table)` yields one Arrow table
        on demand; with `sink`, each masked table is emitted then dropped (outputs
        not accumulated). On success, byte-identical to `run` at lower peak memory.

        If `sink` satisfies `TransactionalSink` (has write/write_batches/
        commit/abort), the run is all-or-nothing: commit on success, abort on
        any exception. A plain Callable sink preserves the pre-existing
        non-transactional contract.
        `quarantine_config` (S2, default None) enforces the same per-row D8
        fail-loud/quarantine rule `run()` enforces, per table, before that
        table's write/eviction. Implemented in execution/_sequential.py; see
        docs/relationships-memory-scaling.md."""
        return _run_sequential(
            self,
            plan,
            source_loader,
            registry=registry,
            pool_cache=pool_cache,
            relationship_graph=relationship_graph,
            namespace_registry=namespace_registry,
            sink=sink,
            quarantine_config=quarantine_config,
        )

    def _resolve_fk_node(
        self,
        node: WorkNode,
        edges: tuple[RelationshipEdge, ...],
        frames: dict[str, pd.DataFrame],
        source_snapshots: dict[tuple[str, str], pd.Series],
        parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]],
        node_by_key: dict[_NodeKey, WorkNode],
        ctx: StrategyContext,
        *,
        key_error_rows: _KeyErrorRows | None = None,
        errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]] | None = None,
    ) -> list[QualityWarning]:
        """Mask an FK child node by mapping its source key through the parent
        source->masked map(s) and applying the OrphanPolicy. Serves scalar FK
        children (1-tuple keys) and composite-FK groups (N-tuple keys).

        Multi-parent (WS5, 2026-06-12): `edges` carries one edge per parent
        the child column-tuple references, in declared config order. The
        maps merge first-hit-wins (setdefault in edge order), so a key in
        two parents resolves to the FIRST declared parent's masked value
        and a row is an orphan only when absent from EVERY parent map.
        The graph guarantees the edges share one orphan policy
        (orphan_policy_conflict); remap minting routes through the first
        edge's parent strategy.

        S2 (quarantine-aware FK resolution): gathers errored parent keys for
        these edges, passes them to `resolve_fk_keys`, and emits one cascaded
        `RowError` per child row whose key was excluded from the parent map
        (parent row-errored) -- drained by the same drain point as any other
        row error on this node."""
        edge = edges[0]
        parent_map = self._parent_map(
            edge,
            frames,
            source_snapshots,
            parent_map_cache,
            key_error_rows=key_error_rows,
            errored_keys_cache=errored_keys_cache,
        )
        if len(edges) > 1:
            merged: dict[_KeyTuple, _KeyTuple] = dict(parent_map)
            for extra_edge in edges[1:]:
                extra_map = self._parent_map(
                    extra_edge,
                    frames,
                    source_snapshots,
                    parent_map_cache,
                    key_error_rows=key_error_rows,
                    errored_keys_cache=errored_keys_cache,
                )
                for key, value in extra_map.items():
                    merged.setdefault(key, value)
            parent_map = merged
        errored_parent_keys = gather_errored_parent_keys(edges, errored_keys_cache)
        child_frame = frames[node.table]
        child_cols = edge.child_columns
        n = len(child_frame)

        # S21 Q7 fix (2026-05-30): batch-materialize each child column to a
        # plain Python list once + compute the row-level null mask once. The
        # prior implementation called `v.iloc[i]` for every (row, column)
        # pair, paying pandas scalar-unboxing overhead O(n*k) times. For a
        # 1M-row child table with a 3-column FK that was ~3M `.iloc[i]`
        # calls. QA Q7 + ISO/IEC 25010 §5.2.2 (performance efficiency).
        col_vals_lists = [child_frame[c].tolist() for c in child_cols]
        na_array = (
            child_frame[list(child_cols)].isna().any(axis=1).to_numpy()
            if len(child_cols) > 1
            else child_frame[child_cols[0]].isna().to_numpy()
        )
        child_keys: list[_KeyTuple | None] = []
        for i in range(n):
            if na_array[i]:
                child_keys.append(None)  # null FK: preserved, never an orphan
            else:
                child_keys.append(tuple(_fk_key_value(col[i]) for col in col_vals_lists))

        remap_fn = make_remap_fn(edge, node_by_key, ctx, self._handlers)
        masked_keys, warnings, cascade = resolve_fk_keys(
            child_keys,
            parent_map,
            edge,
            remap_fn=remap_fn,
            errored_parent_keys=errored_parent_keys or None,
        )

        for j, c in enumerate(child_cols):
            values = [None if mk is None else mk[j] for mk in masked_keys]
            # DE-10: a raw list assignment here is how the pandas route used
            # to silently round an FK key beyond 2**53 -- any None mixed with
            # a big int makes pandas' Series constructor infer float64. Route
            # through the shared lossless-typing contract instead: a pure
            # integer(+null) column builds via pandas' nullable Int64
            # extension dtype (exact, round-trips to Arrow int64); anything
            # else either has no precision to lose (unchanged) or raises the
            # same typed error the out-of-core route already raises for a
            # provably unrepresentable mix.
            safe_ints = lossless_fk_int_values(values)
            child_frame[c] = pd.array(safe_ints, dtype="Int64") if safe_ints is not None else values
        # S2: emit one cascaded RowError per child row whose key was excluded
        # from the parent map (parent key row-errored). The masked cell is
        # already None (set above), so even a downstream bug in quarantine
        # wiring can never durably publish the raw key. Extracted to
        # _orphan.py to keep this module under the LOC cap.
        ctx.row_errors.extend(cascade_row_errors(cascade, child_cols[0]))
        return warnings

    def _parent_map(
        self,
        edge: RelationshipEdge,
        frames: dict[str, pd.DataFrame],
        source_snapshots: dict[tuple[str, str], pd.Series],
        parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]],
        *,
        key_error_rows: _KeyErrorRows | None = None,
        errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]] | None = None,
    ) -> dict[_KeyTuple, _KeyTuple]:
        """Build (cached) the parent source-key -> masked-key map for an edge.

        Source values come from the pre-mask snapshot taken when the parent
        column masked; masked values from the now-mutated parent frame. A parent
        column never masked (no snapshot) maps identity (source == current), which
        is the correct RI behavior for an unmasked parent key.

        S2: `key_error_rows` (default None) is the per-table/per-column row-error
        index. A parent row whose key column row-errored is EXCLUDED from the map
        (never resolves to its raw value); its raw key + trigger goes into
        `errored_keys_cache` (keyed like `parent_map_cache`) for the caller to
        cascade onto referencing children. Byte-parity: empty `key_error_rows`
        for this edge's columns builds the identical pre-fix map.
        """
        cache_key: _NodeKey = (edge.parent_table, edge.parent_columns)
        cached = parent_map_cache.get(cache_key)
        if cached is not None:
            # errored_keys_cache[cache_key] stays in lockstep (same prior call).
            return cached
        ptable = edge.parent_table
        if ptable not in frames:
            parent_map_cache[cache_key] = {}
            if errored_keys_cache is not None:
                errored_keys_cache[cache_key] = {}
            return {}
        masked_frame = frames[ptable]
        pcols = edge.parent_columns
        src_series = [source_snapshots.get((ptable, c), masked_frame[c]) for c in pcols]
        masked_series = [masked_frame[c] for c in pcols]
        n = len(masked_frame)
        # S21 Q7 fix (2026-05-30): batch-materialize src + masked series to
        # plain Python lists once. The prior implementation called
        # `s.iloc[i]` for every (row, column) pair on BOTH src and masked
        # sides, paying pandas scalar-unboxing overhead O(n*2k) times. On a
        # 1M-row parent table with a 3-column FK that was ~6M `.iloc[i]`
        # calls. QA Q7 + ISO/IEC 25010 §5.2.2 (performance efficiency).
        src_lists = [s.tolist() for s in src_series]
        masked_lists = [s.tolist() for s in masked_series]

        # S2: row -> trigger for rows excluded by a key-column row-error
        # (first key-column error wins the trigger for a composite key).
        excluded: dict[int, str] = {}
        if key_error_rows:
            tbl_errs = key_error_rows.get(ptable, {})
            for c in pcols:
                for ridx, trig in tbl_errs.get(c, {}).items():
                    excluded.setdefault(ridx, trig)

        out: dict[_KeyTuple, _KeyTuple] = {}
        errored: dict[_KeyTuple, str] = {}
        for i in range(n):
            raw = [col[i] for col in src_lists]
            if any(pd.isna(x) for x in raw):
                continue  # parent key with a null component cannot be referenced
            src_t = tuple(_fk_key_value(x) for x in raw)
            if i in excluded:
                # EXCLUDE: a row-errored key never enters the resolution map.
                errored.setdefault(src_t, excluded[i])
                continue
            out[src_t] = tuple(col[i] for col in masked_lists)
        parent_map_cache[cache_key] = out
        if errored_keys_cache is not None:
            errored_keys_cache[cache_key] = errored
        return out


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

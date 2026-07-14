"""Batch-streaming runner for the out-of-core FK route.

Executes a relational FK job as bounded batch passes per table, in
FK-topological order, so no whole table is ever resident on the lazy-source
path:

1. Rewrite pass: the raw source streams through `mask_batch` (non-FK
   columns) and one `ChildFkBatchJoiner` per incoming edge (FK columns).
   Every joiner keys off the IMMUTABLE raw batch while its rewrite
   accumulates in the output batch: edges may overlap on a child column, and
   the whole-child contract is that each edge joins the RAW child, with later
   edges overwriting the shared column. The rewritten batches flow straight
   into `TransactionalSink.write_batches` under one analytically fixed
   schema, because an Arrow ParquetWriter writes a single-schema file (the
   same constraint pyarrow.dataset resolves by unifying fragment schemas up
   front). Tables with outgoing edges tee their key columns to a narrow
   staged Parquet copy on the way through (`_stage.py`).
2. Relation build: every outgoing parent-key relation maps the RAW key (the
   join key a child still holds) to the table's FINAL post-rewrite value,
   taken from the staged copy (sink path) or the reassembled resident table
   (no-sink path), never from a re-mask of the raw stream: an incoming edge
   may have rewritten the key column to a value the column's own plan seed
   cannot reproduce, and the published table is the only truth children may
   reference.

Re-reading a source once per pass trades bounded RAM for disk IO, the
standard external-memory discipline (DuckDB's larger-than-memory hash join
re-reads its spilled partitions the same way); a resident `pa.Table` source
re-iterates for free. Without a sink, the streamed batches are reassembled in
memory column-wise under the whole-child chunk-merge rules (`_join.py`), so
the returned tables keep the value-derived column types the pandas-oracle
parity suite pins.
"""

from __future__ import annotations

import heapq
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import NULL_FK_KEY, fk_key_value
from decoy_engine.execution._output_projection import (
    UnconfiguredColumnPolicy,
    enforce_output_projection,
)
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution._transactional_sink import TransactionalSink
from decoy_engine.execution.out_of_core import _join as _join_ooc
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._budget import check_temp_disk_budget
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.execution.out_of_core._emit import (
    assemble_resident,
    emit_to_sink,
    empty_output_table,
)
from decoy_engine.execution.out_of_core._join import orphan_fk_error, orphan_fk_warning
from decoy_engine.execution.out_of_core._mask import (
    mask_batch,
    mask_column,
    masked_output_type,
    table_seed,
)
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_aligned
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.keyprovider import require_mask_key
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import TypeAlias

    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph
    from decoy_engine.relationships._graph import RelationshipEdge

    # One source per table: a resident Arrow table (back-compat) or a
    # path-backed lazy reader (the bounded-residency capability path).
    TableSource: TypeAlias = pa.Table | LazySource


def run_fk_out_of_core(
    plan: Plan,
    sources: Mapping[str, pa.Table | LazySource],
    *,
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
    sink: TransactionalSink | None = None,
    temp_dir: Path | None = None,
    memory_limit: str | None = None,
    batch_rows: int | None = None,
    temp_disk_budget_bytes: int | None = None,
    unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
    key_provider: KeyProvider | None = None,
) -> ExecutionResult:
    """Run the out-of-core FK relationship route as a batch stream.

    Private entrypoint. Handles single- and composite (multi-column) FK edges,
    chains and fanouts across the relationship graph, and any of the admitted
    parent key strategies (hash, redact, truncate, passthrough); see
    `check_out_of_core_compatibility` for the exact admitted surface, which is
    what keeps this byte-identical to the pandas oracle for accepted plans
    EXCEPT the divergences inventoried in `_batch_join.py`'s module docstring
    (fixed-schema typing and composite partial-null orphan handling).
    With a sink, each table is staged as a stream of bounded record batches
    (never whole-table resident); without one, the streamed batches are
    reassembled into in-memory tables with whole-column type semantics.

    `batch_rows` bounds every streamed pass (default: the pinned module
    constant); any legal value is byte-transparent on the output.
    `memory_limit` and `batch_rows` are typically sized together from one
    budget (`_budget.resolve_budget`). `temp_disk_budget_bytes` bounds the
    spill footprint under this run's temp root, checked at each table
    boundary; exceeding it aborts the sink and fails closed.
    """
    if batch_rows is not None and batch_rows < 1:
        raise ExecutionError(
            code="out_of_core_batch_rows_invalid",
            message=f"batch_rows must be a positive row count, got {batch_rows}.",
        )
    # DE-02 (Codex BLOCKER 4): fail-closed gate FIRST -- before any admissibility
    # or source checks -- so a keyed out-of-core job can never run off job_seed at
    # GA regardless of entry point.
    mask_key = require_mask_key(plan, key_provider)
    work = order_work(build_work_list(plan, registry), relationship_graph)
    compat = check_out_of_core_compatibility(plan, work, relationship_graph)
    if not compat.accepted:
        raise ExecutionError(
            code=compat.primary_code or "out_of_core_rejected",
            message=compat.message(),
        )
    for edge in relationship_graph.edges:
        if edge.parent_table not in sources or edge.child_table not in sources:
            raise ExecutionError(
                code="out_of_core_source_missing",
                message="out-of-core FK route requires every parent and child source.",
            )

    owned_temp = temp_dir is None
    root = Path(tempfile.mkdtemp(prefix="decoy-ooc-")) if temp_dir is None else temp_dir
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    committed = False
    try:
        incoming, outgoing = _edge_indexes(relationship_graph)
        parent_relations: dict[RelationshipEdge, ParentKeyRelation] = {}
        outputs: dict[str, pa.Table] = {}
        warnings: list[QualityWarning] = []
        for table_name in _table_order(plan, relationship_graph, sources):
            if table_name not in sources:
                continue
            _stream_table(
                plan,
                table_name,
                sources[table_name],
                incoming_edges=tuple(incoming[table_name]),
                outgoing_edges=tuple(outgoing[table_name]),
                parent_relations=parent_relations,
                temp_dir=root / "joins" / table_name,
                relation_dir=root / "relations" / table_name,
                staging_path=root / "staged" / table_name / "masked_keys.parquet",
                memory_limit=memory_limit,
                batch_rows=batch_rows,
                sink=sink,
                outputs=outputs,
                warnings=warnings,
                unconfigured_column_policy=unconfigured_column_policy,
                mask_key=mask_key,
            )
            if temp_disk_budget_bytes is not None:
                # Table boundaries are the natural checkpoints: the spill
                # footprint peaks with each table's relation/join staging, and
                # a walk here costs a handful of stats, not a watcher thread.
                check_temp_disk_budget(root, max_bytes=temp_disk_budget_bytes)
        if sink is not None:
            sink.commit()
            committed = True
            return ExecutionResult(outputs={}, warnings=tuple(warnings))
        return ExecutionResult(outputs=outputs, warnings=tuple(warnings))
    except Exception:
        if sink is not None and not committed:
            sink.abort()
        raise
    finally:
        # The relation/join staging subtrees hold (source_key, masked_key) pairs
        # on disk and the staged subtree holds output values (which can include
        # preserved raw orphan keys); a caller-supplied temp_dir is NOT torn
        # down (owned_temp below is False), so without this those keys sit at
        # rest after a successful run. Only the parts the runner itself created
        # get wiped here; the rest of a caller-owned root is the caller's
        # responsibility.
        shutil.rmtree(root / "relations", ignore_errors=True)
        shutil.rmtree(root / "joins", ignore_errors=True)
        shutil.rmtree(root / "staged", ignore_errors=True)
        if owned_temp:
            shutil.rmtree(root, ignore_errors=True)


def _stream_table(
    plan: Plan,
    table_name: str,
    raw: TableSource,
    *,
    incoming_edges: tuple[RelationshipEdge, ...],
    outgoing_edges: tuple[RelationshipEdge, ...],
    parent_relations: dict[RelationshipEdge, ParentKeyRelation],
    temp_dir: Path,
    relation_dir: Path,
    staging_path: Path,
    memory_limit: str | None,
    batch_rows: int | None,
    sink: TransactionalSink | None,
    outputs: dict[str, pa.Table],
    warnings: list[QualityWarning],
    unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
    mask_key: bytes | None = None,
) -> None:
    """Rewrite pass for one table: mask + join per batch, then emit.

    Emits either into `sink.write_batches` (bounded residency, fixed schema)
    or into `outputs` (in-memory reassembly with whole-column type semantics).
    Each outgoing parent-key relation is then built from the table's final
    output (staged narrow copy or resident table) and stored into
    `parent_relations` for the children later in the topo order. WARN orphan
    totals are aggregated over the whole stream and appended to `warnings` in
    incoming-edge order, matching whole-table reporting.
    """
    if batch_rows is None:
        batch_rows = _join_ooc._JOIN_BATCH_ROWS
    source_schema = raw.schema
    skip_columns = frozenset(col for edge in incoming_edges for col in edge.child_columns)
    joiners: list[ChildFkBatchJoiner] = []
    try:
        for idx, edge in enumerate(incoming_edges):
            joiners.append(
                _open_joiner(
                    plan,
                    edge,
                    parent_relations[edge],
                    source_schema,
                    temp_dir / f"edge_{idx}",
                    memory_limit,
                    mask_key=mask_key,
                )
            )
        for edge, joiner in zip(incoming_edges, joiners, strict=True):
            if edge.orphan_policy is OrphanPolicy.FAIL:
                _raise_on_total_orphans(edge, joiner, raw, batch_rows)
        fk_components = _fk_component_map(incoming_edges, joiners)
        fixed_schema = _fixed_output_schema(plan, table_name, source_schema, fk_components)
        # DE-03: fail-closed output projection at the earliest point -- the fixed
        # output schema is known before any batch streams. FK-resolved child
        # columns are legitimate output but are not in the mask plan's seed
        # envelope, so they are passed as extra_known to avoid a false positive.
        warnings.extend(
            enforce_output_projection(
                table_name,
                fixed_schema.names,
                plan,
                unconfigured_column_policy,
                extra_known=frozenset(fk_components),
            )
        )
        orphan_totals = [0] * len(joiners)

        def rewritten() -> Iterator[pa.RecordBatch]:
            for raw_batch in _iter_source_batches(raw, batch_rows):
                out = mask_batch(
                    plan, table_name, raw_batch, skip_columns=skip_columns, mask_key=mask_key
                )
                for join_idx, joiner in enumerate(joiners):
                    # key_source pins every join to the immutable raw batch:
                    # edges overlapping on a child column must each key off
                    # the source values, not an earlier edge's rewrite.
                    out, orphans = joiner.join_batch(out, key_source=raw_batch)
                    orphan_totals[join_idx] += orphans
                yield out

        if sink is not None:
            emit_to_sink(
                plan,
                table_name,
                raw,
                rewritten(),
                fixed_schema=fixed_schema,
                fk_components=fk_components,
                outgoing_edges=outgoing_edges,
                parent_relations=parent_relations,
                relation_dir=relation_dir,
                staging_path=staging_path,
                memory_limit=memory_limit,
                batch_rows=batch_rows,
                sink=sink,
                source_schema=source_schema,
            )
        else:
            batches = list(rewritten())
            table_out = (
                assemble_resident(batches, fk_components)
                if batches
                else empty_output_table(plan, table_name, source_schema, fk_components)
            )
            outputs[table_name] = table_out
            for edge in outgoing_edges:
                parent_relations[edge] = build_parent_key_relation_aligned(
                    source_parent=raw,
                    masked_parent=table_out,
                    edge=edge,
                    temp_dir=relation_dir,
                    memory_limit=memory_limit,
                    batch_rows=batch_rows,
                )
        for edge, total in zip(incoming_edges, orphan_totals, strict=True):
            if total and edge.orphan_policy is OrphanPolicy.WARN:
                warnings.append(orphan_fk_warning(edge, total))
    finally:
        for joiner in joiners:
            joiner.close()


def _open_joiner(
    plan: Plan,
    edge: RelationshipEdge,
    relation: ParentKeyRelation,
    source_schema: pa.Schema,
    temp_dir: Path,
    memory_limit: str | None,
    *,
    mask_key: bytes | None = None,
) -> ChildFkBatchJoiner:
    for child_col in edge.child_columns:
        if child_col not in source_schema.names:
            raise ExecutionError(
                code="out_of_core_child_column_missing",
                message=f"child source table has no column {child_col!r}.",
            )
    remap_seeds: tuple[ColumnSeed, ...] | None = None
    if edge.orphan_policy is OrphanPolicy.REMAP:
        seeds: list[ColumnSeed] = []
        for parent_col in edge.parent_columns:
            seed = _column_seed(plan, edge.parent_table, parent_col)
            if seed is None:
                raise ExecutionError(
                    code="out_of_core_parent_seed_missing",
                    message=f"parent key {edge.parent_table}.{parent_col} is not in the plan.",
                )
            seeds.append(seed)
        remap_seeds = tuple(seeds)
    return ChildFkBatchJoiner(
        edge=edge,
        parent_relation=relation,
        child_key_types=tuple(source_schema.field(col).type for col in edge.child_columns),
        temp_dir=temp_dir,
        memory_limit=memory_limit,
        remap_seeds=remap_seeds,
        job_seed=mask_key if mask_key is not None else plan.seed_envelope.job_seed,
    )


def _raise_on_total_orphans(
    edge: RelationshipEdge,
    joiner: ChildFkBatchJoiner,
    raw: TableSource,
    batch_rows: int,
) -> None:
    # FAIL reports the whole child's orphan count before any output exists;
    # the per-batch fail-fast inside join_batch only ever sees the first
    # offending batch, so the total needs its own bounded raw pass.
    total = 0
    for raw_batch in _iter_source_batches(raw, batch_rows):
        total += joiner.count_batch_orphans(raw_batch)
    if total:
        raise orphan_fk_error(edge, total)


def _iter_source_batches(src: TableSource, batch_rows: int) -> Iterator[pa.RecordBatch]:
    if isinstance(src, LazySource):
        yield from src.iter_batches(batch_rows)
        return
    yield from src.to_batches(max_chunksize=batch_rows)


def _fk_component_map(
    incoming_edges: tuple[RelationshipEdge, ...],
    joiners: list[ChildFkBatchJoiner],
) -> dict[str, tuple[ChildFkBatchJoiner, int]]:
    """Map each FK child column to (its joiner, component index).

    Well-defined because the compatibility gate rejects multiple parents for
    one child FK tuple.
    """
    components: dict[str, tuple[ChildFkBatchJoiner, int]] = {}
    for edge, joiner in zip(incoming_edges, joiners, strict=True):
        for idx, child_col in enumerate(edge.child_columns):
            components[child_col] = (joiner, idx)
    return components


def _fixed_output_schema(
    plan: Plan,
    table_name: str,
    source_schema: pa.Schema,
    fk_components: Mapping[str, tuple[ChildFkBatchJoiner, int]],
) -> pa.Schema:
    """One deterministic output schema, resolved before any batch is built.

    FK columns take the joiner's fixed type; plan-masked columns take the
    strategy's analytic output type; everything else keeps its source field
    (metadata included). Masked and FK fields are bare, matching the
    set-column field semantics of the per-batch rewrites.
    """
    seed = table_seed(plan, table_name)
    seeds = dict(seed.per_column) if seed is not None else {}
    fields: list[pa.Field] = []
    for field in source_schema:
        if field.name in fk_components:
            joiner, component = fk_components[field.name]
            fields.append(pa.field(field.name, joiner.output_types[component]))
        elif field.name in seeds:
            fields.append(pa.field(field.name, masked_output_type(seeds[field.name], field.type)))
        else:
            fields.append(field)
    return pa.schema(fields, metadata=source_schema.metadata)


def _remap_values(
    plan: Plan,
    edge: RelationshipEdge,
    source_child: pa.Table,
    *,
    mask_key: bytes | None = None,
) -> tuple[pa.Array, ...]:
    """Whole-child REMAP value minting.

    The runner mints remap values per batch inside the joiner; this is the
    single-shot lowering retained as the executable oracle definition.
    `mask_key` (DE-02) defaults to `job_seed` when absent (byte-identical).
    """
    key = mask_key if mask_key is not None else plan.seed_envelope.job_seed
    remapped: list[pa.Array] = []
    for parent_column, child_column in zip(edge.parent_columns, edge.child_columns, strict=True):
        parent_seed = _column_seed(plan, edge.parent_table, parent_column)
        if parent_seed is None:
            raise ExecutionError(
                code="out_of_core_parent_seed_missing",
                message=f"parent key {edge.parent_table}.{parent_column} is not in the plan.",
            )
        normalized = [
            None if fk_key_value(value) is NULL_FK_KEY else fk_key_value(value)
            for value in source_child.column(child_column).combine_chunks().to_pylist()
        ]
        remapped.append(mask_column(pa.array(normalized, from_pandas=True), parent_seed, key))
    return tuple(remapped)


def _edge_indexes(
    relationship_graph: RelationshipGraph,
) -> tuple[dict[str, list[RelationshipEdge]], dict[str, list[RelationshipEdge]]]:
    incoming: dict[str, list[RelationshipEdge]] = defaultdict(list)
    outgoing: dict[str, list[RelationshipEdge]] = defaultdict(list)
    for edge in relationship_graph.edges:
        incoming[edge.child_table].append(edge)
        outgoing[edge.parent_table].append(edge)
    return incoming, outgoing


def _table_order(
    plan: Plan,
    relationship_graph: RelationshipGraph,
    sources: Mapping[str, pa.Table | LazySource],
) -> list[str]:
    tables = {table for table, _seed in plan.seed_envelope.per_table} | set(sources)
    deps: dict[str, set[str]] = {table: set() for table in tables}
    children: dict[str, set[str]] = defaultdict(set)
    for edge in relationship_graph.edges:
        tables.add(edge.parent_table)
        tables.add(edge.child_table)
        deps.setdefault(edge.parent_table, set())
        deps.setdefault(edge.child_table, set()).add(edge.parent_table)
        children[edge.parent_table].add(edge.child_table)
    ready = [table for table in tables if not deps.get(table)]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        table = heapq.heappop(ready)
        ordered.append(table)
        for child in sorted(children[table]):
            deps[child].discard(table)
            if not deps[child]:
                heapq.heappush(ready, child)
    if len(ordered) != len(tables):
        raise ExecutionError(
            code="out_of_core_relationship_cycle",
            message="out-of-core route requires an acyclic table graph.",
        )
    return ordered


def _column_seed(plan: Plan, table: str, column: str) -> ColumnSeed | None:
    for table_name, candidate_seed in plan.seed_envelope.per_table:
        if table_name != table:
            continue
        for col_name, col_seed in candidate_seed.per_column:
            if col_name == column:
                return col_seed
    return None


__all__ = ["run_fk_out_of_core"]

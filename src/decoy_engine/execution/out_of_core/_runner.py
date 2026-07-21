"""Run-wide orchestration for the out-of-core FK route.

Executes a relational FK job as bounded batch passes per table, in
FK-topological order, so no whole table is ever resident on the lazy-source
path. This module owns the run: topological table order, the parent-key
relation registry, and teardown. One table's rewrite (the three-phase
single-streaming-join driver) lives in `_stream_driver.py`.

1. Rewrite pass (per table, `_stream_driver.stream_table`): the raw source
   streams through masking (non-FK columns) and one streamed FK join per
   incoming edge (FK columns), emitting into `TransactionalSink.write_batches`
   under one analytically fixed schema, or reassembling into a resident table
   with whole-column type semantics. Tables with outgoing edges tee their key
   columns to a narrow staged Parquet copy on the way through (`_stage.py`).
2. Relation build: every outgoing parent-key relation maps the RAW key (the
   join key a child still holds) to the table's FINAL post-rewrite value,
   taken from the staged copy (sink path) or the reassembled resident table
   (no-sink path), never from a re-mask of the raw stream: an incoming edge
   may have rewritten the key column to a value the column's own plan seed
   cannot reproduce, and the published table is the only truth children may
   reference.

Re-reading a source once per pass trades bounded RAM for disk IO, the standard
external-memory discipline (DuckDB's larger-than-memory hash join re-reads its
spilled partitions the same way); a resident `pa.Table` source re-iterates for
free.
"""

from __future__ import annotations

import heapq
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import NULL_FK_KEY, fk_key_value
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core._budget import check_temp_disk_budget
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.execution.out_of_core._mask import mask_column
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_driver import _column_seed, stream_table
from decoy_engine.keyprovider import require_mask_key

if TYPE_CHECKING:
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph
    from decoy_engine.relationships._graph import RelationshipEdge


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
    budget_bytes: int | None = None,
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
    EXCEPT the divergences inventoried in `_fixed_schema_typing.py`'s module
    docstring (fixed-schema typing and composite partial-null orphan handling).
    With a sink, each table is staged as a stream of bounded record batches
    (never whole-table resident); without one, the streamed batches are
    reassembled into in-memory tables with whole-column type semantics.

    `batch_rows` bounds every streamed pass (default: the pinned module
    constant); any legal value is byte-transparent on the output.
    `budget_bytes` (the UNDIVIDED per-run budget) is preferred over the flat
    `memory_limit` string when given: each DuckDB connection this route opens
    is then capped by its own PHASE-local liveness via `_memory_estimate.
    resolve_phase_memory_limits`, not the run's single global-peak divisor.
    `memory_limit` alone is the fallback used when `budget_bytes` is None.
    `temp_disk_budget_bytes` bounds the spill footprint under this run's temp
    root, checked at each table boundary; exceeding it aborts the sink and
    fails closed.
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
        # HIGH-2 remediation (HC-1 slice 1 gap): the code_set corpus-provenance
        # evidence sink, mirroring `StrategyContext.code_set_corpora` on the
        # pandas/sequential routes. Keyed by (table, column) -- two tables can
        # legally declare a same-named code_set column bound to different
        # corpora, and a bare-column key let the second table's stamp silently
        # overwrite the first's.
        code_set_corpora: dict[tuple[str, str], dict[str, Any]] = {}
        for table_name in _table_order(plan, relationship_graph, sources):
            if table_name not in sources:
                continue
            stream_table(
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
                budget_bytes=budget_bytes,
                sink=sink,
                outputs=outputs,
                warnings=warnings,
                unconfigured_column_policy=unconfigured_column_policy,
                mask_key=mask_key,
                code_set_corpora=code_set_corpora,
            )
            if temp_disk_budget_bytes is not None:
                # Table boundaries are the natural checkpoints: the spill
                # footprint peaks with each table's relation/join staging, and a
                # walk here costs a handful of stats, not a watcher thread.
                check_temp_disk_budget(root, max_bytes=temp_disk_budget_bytes)
        quality_metrics: dict[str, Any] = (
            {"code_set_corpora": list(code_set_corpora.values())} if code_set_corpora else {}
        )
        if sink is not None:
            sink.commit()
            committed = True
            return ExecutionResult(
                outputs={}, warnings=tuple(warnings), quality_metrics=quality_metrics
            )
        return ExecutionResult(
            outputs=outputs, warnings=tuple(warnings), quality_metrics=quality_metrics
        )
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


def _remap_values(
    plan: Plan,
    edge: RelationshipEdge,
    source_child: pa.Table,
    *,
    mask_key: bytes | None = None,
) -> tuple[pa.Array, ...]:
    """Whole-child REMAP value minting.

    The runner mints remap values per output batch inside the joiner; this is
    the single-shot lowering retained as the executable oracle definition.
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


__all__ = ["run_fk_out_of_core"]

"""Helpers of the out-of-core batch runner, split out to keep `_runner` under its size bar.

`_runner` and `_stream_driver` import these under their historical private names. Nothing here
decides a route or touches a sink: joiner construction, the total-orphan precount, the batch
iterator over a source, and the fixed output schema.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_resolve import orphan_fk_violation_error
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._mask import masked_output_type, table_seed
from decoy_engine.execution.out_of_core._relation import _column_seed
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from typing import TypeAlias

    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships._graph import RelationshipEdge

    TableSource: TypeAlias = pa.Table | LazySource


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
                code=reasons.runtime.OUT_OF_CORE_CHILD_COLUMN_MISSING,
                message=f"child source table has no column {child_col!r}.",
            )
    remap_seeds: tuple[ColumnSeed, ...] | None = None
    if edge.orphan_policy is OrphanPolicy.REMAP:
        seeds: list[ColumnSeed] = []
        for parent_col in edge.parent_columns:
            seed = _column_seed(plan, edge.parent_table, parent_col)
            if seed is None:
                raise ExecutionError(
                    code=reasons.runtime.OUT_OF_CORE_PARENT_SEED_MISSING,
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
        raise orphan_fk_violation_error(edge, total)


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

    Well-defined because the runner resolves one representative edge per child
    FK tuple (`_multi_parent.resolve_incoming`).
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

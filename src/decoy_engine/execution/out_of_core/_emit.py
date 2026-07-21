"""Final-output emission for the batch-streaming out-of-core FK runner.

Owns everything after a table's rewrite stream exists: reconciling batches
onto the run's analytically fixed schema and streaming them into the
transactional sink (one Parquet writer, one schema, the same constraint
pyarrow.dataset resolves by unifying fragment schemas up front), or
reassembling them into a resident table with whole-column value-derived types
(undoing the fixed schema with Arrow's own permissive field-merge promotion,
`pa.unify_schemas`, exactly as `_join._concat_fk_chunks` pins it). The
outgoing parent-key relations are built here too, because their masked side
must be the FINAL emitted values: the staged narrow copy on the sink path,
the reassembled table otherwise, and a synthesized whole-table-typed empty
output for a zero-row stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution.out_of_core._join import _unified_chunk_type
from decoy_engine.execution.out_of_core._mask import mask_table
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_aligned
from decoy_engine.execution.out_of_core._stage import MaskedKeyStager

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.execution.out_of_core._source import LazySource
    from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships._graph import RelationshipEdge


def emit_to_sink(
    plan: Plan,
    table_name: str,
    raw_parent_source: pa.Table | LazySource,
    rewritten: Iterator[pa.RecordBatch],
    *,
    fixed_schema: pa.Schema,
    fk_components: Mapping[str, tuple[StreamFkJoiner, int]],
    outgoing_edges: tuple[RelationshipEdge, ...],
    parent_relations: dict[RelationshipEdge, ParentKeyRelation],
    relation_dir: Path,
    staging_path: Path,
    memory_limit: str | None,
    batch_rows: int,
    sink: TransactionalSink,
    source_schema: pa.Schema,
    on_stream_consumed: Callable[[], None] | None = None,
    masked_observed_types: Mapping[str, set[pa.DataType]] | None = None,
) -> None:
    """Stream the rewritten batches to the sink, then build outgoing relations.

    A leaf table streams straight through. A table with outgoing edges tees
    its parent-key columns to a narrow staged Parquet copy on the way (still
    bounded: one batch resident at a time, the copy on disk), because the
    relation's masked side must be the FINAL published values, which no
    longer exist anywhere else once the batches are gone into the sink.

    `on_stream_consumed` (default None) fires the instant the rewrite stream is
    fully drained -- after the sink write, before the outgoing relation build.
    The rewrite stream is what holds the incoming joiners' DuckDB connections
    (each a full-`memory_limit` instance materializing its parent relation);
    the relation build opens ANOTHER full-`memory_limit` instance to dedup. If
    the joiners stayed open across that build, two full-budget DuckDB instances
    would be live at once, and under a process-level address-space cap
    (RLIMIT_DATA) their combined reservations overflow it and malloc fails
    before either hits its own internal limit. Releasing the joiners here caps
    the linear chain at one full-budget instance at a time. `_relation_masked_types`
    reads only the joiners' Python-side observed/output types, which survive the
    connection close, so the relation build below is unaffected.

    `masked_observed_types` (default None): the driver's own pre-reconciliation
    per-column type observations (see `MaskedKeyStager`'s docstring for why the
    single-source-read payload store needs this threaded through rather than
    derived from the batches this function sees).
    """
    staged_columns = tuple(sorted({col for edge in outgoing_edges for col in edge.parent_columns}))
    if not staged_columns:
        sink.write_batches(
            table_name,
            (reconcile_batch(batch, fixed_schema) for batch in rewritten),
            schema=fixed_schema,
        )
        if on_stream_consumed is not None:
            on_stream_consumed()
        return
    stager = MaskedKeyStager(
        staging_path,
        columns=staged_columns,
        fixed_schema=fixed_schema,
        masked_observed_types=masked_observed_types,
    )
    try:

        def emitted() -> Iterator[pa.RecordBatch]:
            for batch in rewritten:
                reconciled = reconcile_batch(batch, fixed_schema)
                stager.add(batch, reconciled)
                yield reconciled

        sink.write_batches(table_name, emitted(), schema=fixed_schema)
    finally:
        stager.close()
    # Release the incoming joiners' DuckDB connections BEFORE the relation
    # build opens its own full-budget instance (see the docstring).
    if on_stream_consumed is not None:
        on_stream_consumed()
    if stager.rows:
        for edge in outgoing_edges:
            parent_relations[edge] = build_parent_key_relation_aligned(
                source_parent=raw_parent_source,
                masked_parent=stager.source(),
                edge=edge,
                masked_types=_relation_masked_types(edge, fk_components, stager, fixed_schema),
                temp_dir=relation_dir,
                memory_limit=memory_limit,
                batch_rows=batch_rows,
            )
        return
    # A zero-row parent has no data-derived types; the empty whole-table
    # output carries the exact types a whole-table build would.
    empty = empty_output_table(plan, table_name, source_schema, fk_components)
    for edge in outgoing_edges:
        parent_relations[edge] = build_parent_key_relation_aligned(
            source_parent=raw_parent_source,
            masked_parent=empty,
            edge=edge,
            temp_dir=relation_dir,
            memory_limit=memory_limit,
            batch_rows=batch_rows,
        )


def _relation_masked_types(
    edge: RelationshipEdge,
    fk_components: Mapping[str, tuple[StreamFkJoiner, int]],
    stager: MaskedKeyStager,
    fixed_schema: pa.Schema,
) -> tuple[pa.DataType, ...]:
    """Data-derived whole-column type of each staged parent-key column.

    The staged file carries the fixed analytic schema; the relation must carry
    the type the whole-table build derives from the values (a downstream child
    republishes these scalars, so the type IS the value). The derivation is
    the same chunk-type merge the resident reassembly uses.
    """
    types: list[pa.DataType] = []
    for col in edge.parent_columns:
        if col in fk_components:
            joiner, component = fk_components[col]
            observed = joiner.observed_types[component]
            fixed = joiner.output_types[component]
        else:
            observed = stager.observed[col]
            fixed = fixed_schema.field(col).type
        target = next(iter(observed)) if len(observed) == 1 else _unified_chunk_type(observed)
        types.append(fixed if target is None else target)
    return tuple(types)


def empty_output_table(
    plan: Plan,
    table_name: str,
    source_schema: pa.Schema,
    fk_components: Mapping[str, tuple[StreamFkJoiner, int]],
) -> pa.Table:
    """Zero-row output with whole-table column types.

    An empty stream has no batches to derive types from, and the analytic
    fixed schema is NOT what a whole-table build yields: value-inferring
    kernels type an empty column null, and an FK column concatenates zero
    chunks into a null-typed empty array. Reproduce both exactly.
    """
    table = mask_table(
        plan,
        table_name,
        source_schema.empty_table(),
        skip_columns=frozenset(fk_components),
    )
    for col in fk_components:
        idx = table.schema.get_field_index(col)
        table = table.set_column(idx, col, pa.array([], from_pandas=True))
    return table


def reconcile_batch(batch: pa.RecordBatch, fixed_schema: pa.Schema) -> pa.RecordBatch:
    """Cast one rewritten batch onto the fixed schema.

    The only cast this performs in practice is null-typed -> fixed type: a
    redact strategy over an all-null batch infers a null column where the
    fixed schema carries the replacement scalar's type, and casting an
    all-null array is lossless.
    """
    arrays = [
        column if column.type.equals(field.type) else column.cast(field.type)
        for column, field in zip(batch.columns, fixed_schema, strict=True)
    ]
    return pa.record_batch(arrays, schema=fixed_schema)


def assemble_resident(
    batches: list[pa.RecordBatch],
    fk_components: Mapping[str, tuple[StreamFkJoiner, int]],
) -> pa.Table:
    """Reassemble streamed batches into a table with whole-column types.

    Byte-parity with whole-table execution requires undoing the fixed
    schema's schema-level typing where whole-column inference differs: non-FK
    masked columns merge their per-batch chunk types (the same permissive
    merge `_concat_fk_chunks` pins against whole-column inference), and FK
    columns replay the merge over the joiner's observed pre-cast chunk types,
    recovering the value-derived narrowing a fixed schema cannot know.
    Callers handle the empty stream (`empty_output_table`); at least one
    batch is required here.
    """
    template = batches[0].schema
    columns: list[pa.ChunkedArray] = []
    fields: list[pa.Field] = []
    for idx, name in enumerate(template.names):
        chunks = [batch.column(idx) for batch in batches]
        if name in fk_components:
            joiner, component = fk_components[name]
            column = _fk_resident_column(chunks, joiner.observed_types[component])
        else:
            column = _merged_column(chunks)
        columns.append(column)
        field = template.field(idx)
        fields.append(field if field.type.equals(column.type) else pa.field(name, column.type))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields, metadata=template.metadata))


def _merged_column(chunks: list[pa.Array]) -> pa.ChunkedArray:
    types = {chunk.type for chunk in chunks}
    if len(types) > 1:
        target = _unified_chunk_type(types)
        if target is not None:
            chunks = [chunk.cast(target) for chunk in chunks]
    return pa.chunked_array(chunks)


def _fk_resident_column(chunks: list[pa.Array], observed: set[pa.DataType]) -> pa.ChunkedArray:
    """Recover the whole-column FK type from the joiner's pre-cast chunk types.

    Every chunk is already cast to the joiner's fixed type, so the values are
    the whole-child values; only the Arrow type may need narrowing (all
    orphan-normalized integral values under a float64 fixed type) or nulling
    (an all-null child column, which whole-column inference leaves untyped).
    Both casts are lossless by construction.
    """
    column = pa.chunked_array(chunks)
    if not observed:
        return column
    target = next(iter(observed)) if len(observed) == 1 else _unified_chunk_type(observed)
    if target is None or target.equals(column.type):
        return column
    if pa.types.is_null(target):
        return pa.chunked_array([pa.nulls(len(chunk)) for chunk in chunks])
    return column.cast(target)


__all__ = ["assemble_resident", "emit_to_sink", "empty_output_table", "reconcile_batch"]

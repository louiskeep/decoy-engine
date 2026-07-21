"""Three-phase batch-streaming driver for one out-of-core FK table (OOC-B).

Split out of `_runner.py` (which sits at its module-size ceiling) so the
single-streaming-join restructure has room. `_runner.py` owns run-wide
orchestration (topological table order, relation registry, run teardown);
this module owns ONE table's rewrite.

The fix#1 shape: rather than fusing the FK join into the per-batch mask loop
(one `LEFT JOIN` per child batch against a materialized parent, the removed
`ChildFkBatchJoiner`), each table streams in THREE phases so the per-edge join
is a single streamed operation with no O(distinct-parent-key) resident
structure:

1. Key pre-pass. One pass over the raw child stages every incoming edge's
   `(row_nr, join_key, src)` keys into that edge's spillable `child_keys` TEMP
   TABLE (`StreamFkJoiner.stage_batch`), numbering rows globally. FAIL policies
   run their anti-join precount here and raise before any output.
2. One streamed join per edge. Each edge opens its ordered FK-output reader
   (`StreamFkJoiner.iter_output`), wrapped in an `FkOutputCursor`.
3. Mask pass, zipped to the sink. A SECOND pass over the raw child masks the
   non-FK columns per batch; each edge's cursor supplies exactly that batch's
   FK values (slicing across the join reader's own batch boundaries), the FK
   columns are overwritten, and the batch is emitted. The cursor's row_nr
   alignment guard fails closed if the two source re-reads ever disagree.

The cost is two READS of the source bytes (not double masking): masking runs
once, in phase 3. Every heavy relational operation (the join, its spill, the
sort) is delegated to DuckDB via `StreamFkJoiner`; nothing here re-implements
memory management. Byte-parity to the pandas oracle is unchanged and pinned by
`tests/parity/`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._output_projection import (
    UnconfiguredColumnPolicy,
    enforce_output_projection,
)
from decoy_engine.execution.out_of_core import _join as _join_ooc
from decoy_engine.execution.out_of_core._emit import (
    assemble_resident,
    emit_to_sink,
    empty_output_table,
)
from decoy_engine.execution.out_of_core._join import orphan_fk_error, orphan_fk_warning
from decoy_engine.execution.out_of_core._mask import mask_batch, masked_output_type, table_seed
from decoy_engine.execution.out_of_core._memory_estimate import resolve_phase_memory_limits
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_aligned
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_join import FkOutputCursor, StreamFkJoiner
from decoy_engine.relationships._graph import OrphanPolicy
from decoy_engine.transforms.code_set import (
    CodeSetConfig,
    describe_loaded_corpus,
    resolve_corpus_record,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path
    from typing import TypeAlias

    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.relationships._graph import RelationshipEdge
    from decoy_engine.transforms._codeset_loader import _CorpusRecord

    # One source per table: a resident Arrow table (back-compat) or a
    # path-backed lazy reader (the bounded-residency capability path).
    TableSource: TypeAlias = pa.Table | LazySource


def stream_table(
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
    budget_bytes: int | None,
    sink: TransactionalSink | None,
    outputs: dict[str, pa.Table],
    warnings: list[QualityWarning],
    unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
    mask_key: bytes | None = None,
    code_set_corpora: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> None:
    """Rewrite one table via the three-phase single-streaming-join driver.

    Emits either into `sink.write_batches` (bounded residency, fixed schema) or
    into `outputs` (in-memory reassembly with whole-column type semantics).
    Each outgoing parent-key relation is built from the table's final output and
    stored into `parent_relations` for later children. WARN orphan totals are
    aggregated over the whole stream and appended to `warnings` in
    incoming-edge order. `budget_bytes` (when not None) sizes the joiner/build
    DuckDB connections by their own phase-local liveness (see
    `_memory_estimate.resolve_phase_memory_limits`).

    `code_set_corpora` (default None): the shared corpus-provenance evidence
    sink, mutated in place. Each column's evidence commits only once its stream
    has seen a value the mask kernel would dispatch (missing == null OR float
    NaN, per the kernel's `_is_missing`), matching `CodeSetHandler.run`'s
    masked_any gate. The corpus record is resolved ONCE here and threaded into
    every `mask_batch` call AND the stamp, so a mid-stream corpus swap cannot
    make batches or the stamp disagree on version.
    """
    if batch_rows is None:
        batch_rows = _join_ooc._JOIN_BATCH_ROWS
    source_schema = raw.schema
    skip_columns = frozenset(col for edge in incoming_edges for col in edge.child_columns)
    code_set_corpus_records, table_code_set_evidence = _code_set_records_and_evidence_for_table(
        plan, table_name, source_schema.names, skip_columns=skip_columns
    )
    # Evidence is resolved from plan+schema before any row is read; the commit
    # is deferred until the stream has seen a non-missing value per column.
    code_set_null_seen: dict[str, bool] = dict.fromkeys(code_set_corpus_records, False)
    # Phase-local caps (Part A + HIGH): a resident-path joiner stays live
    # through this table's own build, so it opens at the build's cap; sink-ness
    # selects the joiner cap here since memory_limit is fixed at open.
    sink_joiner, resident_joiner, sink_build_memory_limit, resident_build_memory_limit = (
        resolve_phase_memory_limits(
            budget_bytes=budget_bytes,
            memory_limit=memory_limit,
            incoming_edges=len(incoming_edges),
            sink=sink is not None,
        )
    )
    joiner_memory_limit = sink_joiner if sink is not None else resident_joiner
    joiners: list[StreamFkJoiner] = []
    try:
        # --- Phase 1: key pre-pass. One raw source pass stages every edge. ---
        for idx, edge in enumerate(incoming_edges):
            joiner = _open_joiner(
                plan,
                edge,
                parent_relations[edge],
                source_schema,
                temp_dir / f"edge_{idx}",
                joiner_memory_limit,
                mask_key=mask_key,
            )
            joiner.begin_staging()
            joiners.append(joiner)
        for raw_batch in _iter_source_batches(raw, batch_rows):
            for joiner in joiners:
                joiner.stage_batch(raw_batch)
        # FAIL reports the whole child's orphan count before any output exists.
        for edge, joiner in zip(incoming_edges, joiners, strict=True):
            if edge.orphan_policy is OrphanPolicy.FAIL:
                total = joiner.total_orphans()
                if total:
                    raise orphan_fk_error(edge, total)

        fk_components = _fk_component_map(incoming_edges, joiners)
        fixed_schema = _fixed_output_schema(plan, table_name, source_schema, fk_components)
        # DE-03: fail-closed output projection at the earliest point -- the fixed
        # output schema is known before any batch streams. FK-resolved child
        # columns are legitimate output but not in the mask plan's seed envelope,
        # so they are passed as extra_known to avoid a false positive.
        warnings.extend(
            enforce_output_projection(
                table_name,
                fixed_schema.names,
                plan,
                unconfigured_column_policy,
                extra_known=frozenset(fk_components),
            )
        )

        # --- Phase 2: open each edge's ordered FK-output cursor. ---
        cursors: list[FkOutputCursor] = [
            FkOutputCursor(joiner.iter_output(batch_rows), edge.child_columns, joiner.output_types)
            for edge, joiner in zip(incoming_edges, joiners, strict=True)
        ]

        # --- Phase 3: mask pass, zipped row_nr-aligned to the sink. ---
        def rewritten() -> Iterator[pa.RecordBatch]:
            for raw_batch in _iter_source_batches(raw, batch_rows):
                for column, seen in code_set_null_seen.items():
                    if seen:
                        continue
                    idx = raw_batch.schema.get_field_index(column)
                    if idx < 0:
                        continue
                    col = raw_batch.column(idx)
                    # "Missing" == null OR float NaN, matching the mask kernel's
                    # `_is_missing`: an all-NaN float column masks nothing, so a
                    # plain null_count would over-stamp evidence it never emits.
                    n_missing = col.null_count
                    if pa.types.is_floating(col.type):
                        # pc.* funcs are dynamically generated; stubs miss them.
                        n_missing += pc.sum(pc.is_nan(col)).as_py() or 0  # type: ignore[attr-defined, unused-ignore]
                    if n_missing < raw_batch.num_rows:
                        code_set_null_seen[column] = True
                out = mask_batch(
                    plan,
                    table_name,
                    raw_batch,
                    skip_columns=skip_columns,
                    mask_key=mask_key,
                    code_set_corpus_records=code_set_corpus_records,
                )
                for edge, joiner, cursor in zip(incoming_edges, joiners, cursors, strict=True):
                    # Each edge's FK values were staged from the RAW child in
                    # phase 1, so overlapping edges never key off an earlier
                    # edge's rewrite; a later edge still overwrites the shared
                    # column last, matching the whole-child contract.
                    fk_arrays = cursor.take(out.num_rows)
                    out = _replace_fk_columns(
                        out, edge.child_columns, fk_arrays, joiner.output_types
                    )
                yield out
            # Row_nr alignment backbone: the mask stream must have consumed
            # every FK output row (neither longer nor shorter than the join).
            for cursor in cursors:
                cursor.assert_exhausted()

        def _release_joiners() -> None:
            # Free joiner DuckDB instances before the relation build (emit_to_sink).
            for joiner in joiners:
                joiner.close()

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
                memory_limit=sink_build_memory_limit,
                batch_rows=batch_rows,
                sink=sink,
                source_schema=source_schema,
                on_stream_consumed=_release_joiners,
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
                    memory_limit=resident_build_memory_limit,
                    batch_rows=batch_rows,
                )
        # Deferred commit: `rewritten()` is fully consumed by now, so
        # `code_set_null_seen` reflects the whole stream. Only a column that saw
        # a non-missing value earns its stamp (masked_any parity).
        if code_set_corpora is not None:
            for key, evidence in table_code_set_evidence.items():
                if code_set_null_seen.get(key[1], False):
                    code_set_corpora[key] = evidence
        for edge, joiner in zip(incoming_edges, joiners, strict=True):
            if joiner.orphan_total and edge.orphan_policy is OrphanPolicy.WARN:
                warnings.append(orphan_fk_warning(edge, joiner.orphan_total))
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
) -> StreamFkJoiner:
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
    return StreamFkJoiner(
        edge=edge,
        parent_relation=relation,
        child_key_types=tuple(source_schema.field(col).type for col in edge.child_columns),
        temp_dir=temp_dir,
        memory_limit=memory_limit,
        remap_seeds=remap_seeds,
        job_seed=mask_key if mask_key is not None else plan.seed_envelope.job_seed,
    )


def _iter_source_batches(src: TableSource, batch_rows: int) -> Iterator[pa.RecordBatch]:
    if isinstance(src, LazySource):
        yield from src.iter_batches(batch_rows)
        return
    yield from src.to_batches(max_chunksize=batch_rows)


def _fk_component_map(
    incoming_edges: tuple[RelationshipEdge, ...],
    joiners: list[StreamFkJoiner],
) -> dict[str, tuple[StreamFkJoiner, int]]:
    """Map each FK child column to (its joiner, component index).

    Well-defined because the compatibility gate rejects multiple parents for
    one child FK tuple.
    """
    components: dict[str, tuple[StreamFkJoiner, int]] = {}
    for edge, joiner in zip(incoming_edges, joiners, strict=True):
        for idx, child_col in enumerate(edge.child_columns):
            components[child_col] = (joiner, idx)
    return components


def _fixed_output_schema(
    plan: Plan,
    table_name: str,
    source_schema: pa.Schema,
    fk_components: Mapping[str, tuple[StreamFkJoiner, int]],
) -> pa.Schema:
    """One deterministic output schema, resolved before any batch is built.

    FK columns take the joiner's fixed type; plan-masked columns take the
    strategy's analytic output type; everything else keeps its source field
    (metadata included). Masked and FK fields are bare, matching the set-column
    field semantics of the per-batch rewrites.
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


def _replace_fk_columns(
    batch: pa.RecordBatch,
    child_columns: tuple[str, ...],
    fk_arrays: tuple[pa.Array, ...],
    output_types: tuple[pa.DataType, ...],
) -> pa.RecordBatch:
    """Overwrite one batch's FK columns with the cursor-supplied FK output.

    Same field semantics as the removed per-batch joiner's `_replace_fk_columns`
    (and `Table.set_column` with a bare name): each field follows the new
    array's fixed type.
    """
    fields = list(batch.schema)
    arrays = list(batch.columns)
    for child_col, fk_array, dtype in zip(child_columns, fk_arrays, output_types, strict=True):
        idx = batch.schema.get_field_index(child_col)
        arrays[idx] = fk_array
        fields[idx] = pa.field(child_col, dtype)
    return pa.record_batch(arrays, schema=pa.schema(fields, metadata=batch.schema.metadata))


def _code_set_records_and_evidence_for_table(
    plan: Plan,
    table_name: str,
    column_names: Sequence[str],
    *,
    skip_columns: frozenset[str],
) -> tuple[dict[str, _CorpusRecord], dict[tuple[str, str], dict[str, Any]]]:
    """code_set corpus record + provenance evidence for one table.

    Mirrors `CodeSetHandler.run`'s once-per-(table, column) stamp (counts +
    identifiers only, no raw codes) so the out-of-core route surfaces the same
    `code_set_corpora` block the pandas/sequential routes merge into
    quality_metrics. Keyed by (table, column) -- two tables may bind a
    same-named code_set column to different corpora -- and each evidence dict
    carries its own table/column identity, since the flattened metrics list
    discards the sink's keys. Restricted to columns present in this table's
    schema and not consumed as an FK child. Returns CANDIDATE evidence (keyed on
    schema presence, not observed masking); `stream_table` withholds the stamp
    for a column that masks nothing. The pinned `_CorpusRecord` is resolved ONCE
    and returned alongside its evidence, then threaded into every `mask_batch`
    call, so a mid-stream corpus swap cannot make masking and evidence disagree.
    """
    seed = table_seed(plan, table_name)
    if seed is None:
        return {}, {}
    names = frozenset(column_names)
    records: dict[str, _CorpusRecord] = {}
    corpora: dict[tuple[str, str], dict[str, Any]] = {}
    for column, column_seed in seed.per_column:
        if column_seed.strategy != "code_set":
            continue
        if column not in names or column in skip_columns:
            continue
        code_cfg = CodeSetConfig.from_dict(provider_config_to_dict(column_seed.provider_config))
        record = resolve_corpus_record(code_cfg)
        records[column] = record
        evidence = describe_loaded_corpus(code_cfg, record=record)
        corpora[(table_name, column)] = {**evidence, "table": table_name, "column": column}
    return records, corpora


def _column_seed(plan: Plan, table: str, column: str) -> ColumnSeed | None:
    for table_name, candidate_seed in plan.seed_envelope.per_table:
        if table_name != table:
            continue
        for col_name, col_seed in candidate_seed.per_column:
            if col_name == column:
                return col_seed
    return None


__all__ = ["stream_table"]

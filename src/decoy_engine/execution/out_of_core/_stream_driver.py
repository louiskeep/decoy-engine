"""Three-phase batch-streaming driver for one out-of-core FK table (OOC-B).

Split out of `_runner.py` (which sits at its module-size ceiling) so the
single-streaming-join restructure has room. `_runner.py` owns run-wide
orchestration (topological table order, relation registry, run teardown);
this module owns ONE table's rewrite.

Single-source-read shape (supersedes the earlier two-read design after a
cross-model gate found it could silently misalign FK-to-row on a same-count
source permutation, and separately shift the value-inference batch boundary):
the raw child is read exactly ONCE. Masking and FK-key staging fuse into that
one pass; FK resolution runs later, from an artifact of that SAME read, never
from the source again.

1. Phase 1 -- the ONLY raw source pass. Per raw batch: stage every incoming
   edge's `(row_nr, join_key, src)` keys into that edge's `SpillChildKeys`
   Arrow-IPC file (`StreamFkJoiner.stage_batch`), mask the non-FK columns
   (`mask_batch`), and append the masked batch to the **payload store**
   (`ResidentPayloadStore` in memory when there is no sink, `SpillPayloadStore`
   -- a lossless Arrow-IPC record-batch stream -- when there is). FAIL
   policies run their anti-join precount here and raise before any output.
2. Phase 2 -- one streamed join per edge. Each edge opens its ordered raw
   join-row reader (`StreamFkJoiner.iter_join_rows`), wrapped in a
   `JoinRowCursor`.
3. Phase 3 -- resolve from the payload store, no source read. For each masked
   payload batch, `cursor.take(m, row_nr_start)` pulls exactly that batch's
   raw join rows (asserting their row_nr against the payload's OWN row_nr --
   two artifacts of the same read, not a hope that two reads agree), then
   `joiner.resolve_batch` produces that batch's FK output at the SAME
   source-chunk granularity phase 1 masked at (the boundary `main` used),
   which the FK columns overwrite before the batch is emitted.

Every heavy relational operation (the join, its spill, the sort) is delegated
to DuckDB via `StreamFkJoiner`; nothing here re-implements memory management.
Byte-parity to the pandas oracle is unchanged and pinned by `tests/parity/`.
"""

from __future__ import annotations

import shutil
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
from decoy_engine.execution.out_of_core._budget import check_temp_disk_budget
from decoy_engine.execution.out_of_core._emit import (
    assemble_resident,
    emit_to_sink,
    empty_output_table,
    reconcile_batch,
)
from decoy_engine.execution.out_of_core._join import orphan_fk_error, orphan_fk_warning
from decoy_engine.execution.out_of_core._mask import mask_batch, masked_output_type, table_seed
from decoy_engine.execution.out_of_core._memory_estimate import resolve_phase_memory_limits
from decoy_engine.execution.out_of_core._payload_store import (
    PayloadStore,
    RawParentKeySpill,
    ResidentPayloadStore,
    SpillPayloadStore,
)
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_aligned
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_join import JoinRowCursor, StreamFkJoiner
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
    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation, ParentSource
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.relationships._graph import RelationshipEdge
    from decoy_engine.transforms._codeset_loader import _CorpusRecord

    # One source per table: a resident Arrow table (back-compat) or a
    # path-backed lazy reader (the bounded-residency capability path).
    TableSource: TypeAlias = pa.Table | LazySource


# How many phase-1 raw batches pass between mid-table disk checkpoints. The
# runner's own boundary check (`_runner.py`, after `stream_table` returns) is
# free -- it runs once per table -- but a checkpoint INSIDE phase 1 costs one
# `os.walk`-shaped disk-usage scan per firing, so this is a batch count, not
# every batch: fine-grained enough to catch a real table (thousands of
# batches) exhausting disk long before its own boundary, coarse enough that
# the scan cost stays a rounding error against the batch work itself.
_DISK_CHECKPOINT_BATCH_INTERVAL = 32


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
    temp_disk_budget_bytes: int | None = None,
    disk_check_root: Path | None = None,
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

    `temp_disk_budget_bytes` + `disk_check_root` (both default None): the SAME
    budget and root `_runner.py` checks at each table boundary, threaded here
    so phase 1 can ALSO checkpoint every `_DISK_CHECKPOINT_BATCH_INTERVAL`
    raw batches -- the table-boundary check alone fires only after a whole
    table's `child_keys.arrow` + `raw_parent_keys.arrow` + `payload.arrow`
    spills have already accumulated, which for one large table can exhaust
    disk long before that boundary is ever reached. `disk_check_root` is the
    RUN's overall temp root (not this table's own `temp_dir`), matching what
    the boundary check measures: the combined footprint of every spill under
    the run, not just this table's.

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
    # A masked (non-FK) column's per-batch inferred type can vary (e.g. redact
    # infers null on an all-null batch, the replacement scalar's type on any
    # other), which the resident path's own narrowing handles later -- but the
    # Arrow IPC spill format fixes ONE schema for its whole stream, so the
    # SINK path's payload batches must already agree before they are stored.
    # FK child columns are untouched here (still raw, `mask_batch` skips them;
    # phase 3 overwrites them with the resolved FK arrays after read-back).
    payload_schema = _payload_schema(plan, table_name, source_schema, skip_columns)
    # Reconciling to `payload_schema` erases the true per-batch masked type an
    # outgoing relation's narrowing needs (an all-null column stays null-typed
    # only if nothing ever re-derives the fixed analytic type from it); track
    # the TRUE natural type here, before reconciliation, so it can be handed
    # to `emit_to_sink` instead of losing it to the payload store's fixed
    # spill schema. See `MaskedKeyStager`'s docstring for the full reasoning.
    outgoing_parent_columns = frozenset(
        col for edge in outgoing_edges for col in edge.parent_columns
    )
    # Sorted for a deterministic spill schema; also the exact set of raw
    # columns the outgoing-relation build needs (see the phase-1 capture
    # below), never a whole-column re-read of `raw`.
    outgoing_parent_column_order = tuple(sorted(outgoing_parent_columns))
    payload_masked_observed: dict[str, set[pa.DataType]] = {
        col: set() for col in outgoing_parent_columns
    }
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
    store: PayloadStore = (
        ResidentPayloadStore() if sink is None else SpillPayloadStore(temp_dir / "payload.arrow")
    )
    # The outgoing-relation build's `source_parent`: a phase-1 capture of the
    # raw outgoing-edge parent-key columns, in phase-1 read order (== the
    # masked-output order), never a second read of `raw` itself (that second
    # read is the BLOCKER a same-count on-disk permutation of `raw` after
    # phase 1 could silently misalign positionally against the masked output;
    # see this module's docstring). `raw` is a harmless default when there are
    # no outgoing edges, since the relation build then never runs.
    raw_parent_source: ParentSource = raw
    raw_parent_spill: RawParentKeySpill | None = None
    raw_parent_projection_schema: pa.Schema | None = None
    if outgoing_parent_column_order:
        raw_parent_projection_schema = pa.schema(
            [source_schema.field(col) for col in outgoing_parent_column_order]
        )
        # Arrow IPC, not Parquet: a key column may carry an Arrow type Parquet
        # cannot encode (e.g. month_day_nano_interval) yet the route admits and
        # the oracle masks; IPC round-trips the full admitted surface losslessly.
        raw_parent_spill = RawParentKeySpill(
            temp_dir / "raw_parent_keys.arrow", raw_parent_projection_schema
        )
        raw_parent_source = raw_parent_spill
    try:
        # --- Phase 1: the ONLY raw source pass. Stages every edge's keys AND
        # masks the non-FK columns into the payload store, keyed by the SAME
        # __decoy_row_nr the FK join numbers by. Runs unconditionally (even
        # with no incoming edges) because masking, not just key staging, needs
        # this one pass now. ---
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
            # Register for cleanup BEFORE begin_staging so a DDL/temp-dir failure
            # there cannot leak the just-opened DuckDB connection.
            joiners.append(joiner)
            joiner.begin_staging()
        batches_since_disk_check = 0
        for raw_batch in _iter_source_batches(raw, batch_rows):
            for joiner in joiners:
                joiner.stage_batch(raw_batch)
            if raw_parent_spill is not None and raw_batch.num_rows:
                raw_parent_spill.append(
                    pa.record_batch(
                        [
                            raw_batch.column(raw_batch.schema.get_field_index(col))
                            for col in outgoing_parent_column_order
                        ],
                        schema=raw_parent_projection_schema,
                    )
                )
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
            masked_batch = mask_batch(
                plan,
                table_name,
                raw_batch,
                skip_columns=skip_columns,
                mask_key=mask_key,
                code_set_corpus_records=code_set_corpus_records,
            )
            for col in payload_masked_observed:
                idx = masked_batch.schema.get_field_index(col)
                if idx >= 0:
                    payload_masked_observed[col].add(masked_batch.column(idx).type)
            if sink is not None:
                # Resident path keeps the naturally-inferred per-batch types
                # (assemble_resident narrows from them); the spill path needs
                # one stable schema across every write to its Arrow IPC stream.
                masked_batch = reconcile_batch(masked_batch, payload_schema)
            store.append(masked_batch)
            if temp_disk_budget_bytes is not None and disk_check_root is not None:
                # Mid-table checkpoint (Task 5): the runner's own boundary
                # check only fires after this whole table's phase 1 has
                # already finished, by which point child_keys.arrow +
                # raw_parent_keys.arrow + payload.arrow may already have
                # exhausted disk on a table with many batches. Same fail-
                # closed error the boundary check raises; a caller cannot
                # tell which checkpoint fired, nor does it need to.
                batches_since_disk_check += 1
                if batches_since_disk_check >= _DISK_CHECKPOINT_BATCH_INTERVAL:
                    check_temp_disk_budget(disk_check_root, max_bytes=temp_disk_budget_bytes)
                    batches_since_disk_check = 0
        # Finalize the spill now: phase 1 is done reading `raw`, and the
        # outgoing-relation build (below, or in `emit_to_sink`) reads this
        # stream back via `raw_parent_source` -- it must carry its end-of-stream
        # marker before any reader opens it.
        if raw_parent_spill is not None:
            raw_parent_spill.finalize()
        # Same reasoning, per edge: each joiner's child-key spill must carry
        # its end-of-stream marker before total_orphans (below) or
        # iter_join_rows (phase 2) opens the first reader over it.
        for joiner in joiners:
            joiner.finalize_staging()
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

        # --- Phase 2: open each edge's ordered raw join-row cursor. ---
        cursors: list[JoinRowCursor] = [
            JoinRowCursor(joiner.iter_join_rows(batch_rows), edge.child_columns)
            for edge, joiner in zip(incoming_edges, joiners, strict=True)
        ]

        # --- Phase 3: resolve FK columns from the payload store; no source read. ---
        def rewritten() -> Iterator[pa.RecordBatch]:
            for row_nr_start, masked_batch in store.iter_batches():
                out = masked_batch
                for edge, joiner, cursor in zip(incoming_edges, joiners, cursors, strict=True):
                    # Each edge's FK values were staged from the RAW child in
                    # phase 1, so overlapping edges never key off an earlier
                    # edge's rewrite; a later edge still overwrites the shared
                    # column last, matching the whole-child contract. `take`
                    # asserts the join reader's row_nr against this SAME
                    # payload batch's row_nr -- both artifacts of one read.
                    join_rows = cursor.take(out.num_rows, row_nr_start)
                    fk_arrays = joiner.resolve_batch(join_rows)
                    out = _replace_fk_columns(
                        out, edge.child_columns, fk_arrays, joiner.output_types
                    )
                yield out
            # Row_nr alignment backbone: the payload store must have consumed
            # every join row (neither longer nor shorter than the join).
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
                raw_parent_source,
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
                masked_observed_types=payload_masked_observed,
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
                    source_parent=raw_parent_source,
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
        # Guard only, mirroring the raw_parent_spill re-finalize below: the
        # normal path already finalizes every joiner's child-key spill right
        # after phase 1. This catches an exception raised mid-phase-1 (a
        # LATER batch in the same table's own loop, or FAIL's orphan-count
        # SELECT), where that finalize call was never reached;
        # `finalize_staging` is idempotent, so re-running it here is safe
        # whether or not the primary call already ran.
        for joiner in joiners:
            joiner.finalize_staging()
            joiner.close()
        store.close()
        # Guard only: the normal path already finalizes this right after phase 1
        # (before its stream is read back). This catches an exception raised
        # mid-phase-1, where that finalize was never reached; `finalize` is
        # idempotent.
        if raw_parent_spill is not None:
            raw_parent_spill.finalize()
        # Early delete (Task 5): every spill this call created --
        # child_keys.arrow per edge, raw_parent_keys.arrow, payload.arrow --
        # lives under this table's OWN `temp_dir` and has had its last read by
        # this point (success or failure), so there is no reason to leave it
        # on disk competing with every LATER table's spill budget until the
        # whole run's teardown. Best-effort: a table this call never reached
        # (an exception before `temp_dir` was even created) leaves nothing to
        # remove.
        shutil.rmtree(temp_dir, ignore_errors=True)


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


def _payload_schema(
    plan: Plan,
    table_name: str,
    source_schema: pa.Schema,
    skip_columns: frozenset[str],
) -> pa.Schema:
    """The phase-1 masked-payload schema: FK columns untouched, others fixed.

    Mirrors `mask_batch`'s own skip logic exactly (never `_fixed_output_schema`'s
    FK substitution, which needs joiner output types this schema is built
    without any batch data): an FK child column is skipped by masking and
    stays at its raw source type until phase 3 overwrites it, so reconciling
    it here would risk a cast against a value that is about to be discarded.
    """
    seed = table_seed(plan, table_name)
    seeds = dict(seed.per_column) if seed is not None else {}
    fields: list[pa.Field] = []
    for field in source_schema:
        if field.name in skip_columns:
            fields.append(field)
        elif field.name in seeds:
            fields.append(pa.field(field.name, masked_output_type(seeds[field.name], field.type)))
        else:
            fields.append(field)
    return pa.schema(fields, metadata=source_schema.metadata)


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

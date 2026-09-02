"""Three-phase sequential per-edge reorder driver for one out-of-core FK table.

Adapts the salvage `_stream_driver.py` (`fix/ooc-b-memory-streaming-join`)
structure to `StreamFkJoiner.run_ordered_join` (the A.3 order-restore consumer)
instead of that branch's `iter_join_rows` (a DuckDB `ORDER BY` global sort). The
salvage driver's phase 3 predates `run_ordered_join`; this module is a
deliberate re-adaptation, not a mechanical port -- see
`docs/plans/2026-09-02-p4-task6-reorder-driver.md` for the FRAME recon that
established the deltas.

Single-source-read shape (unchanged from the salvage driver, still the correct
design for the same reason: a two-read design can silently misalign FK-to-row
on a same-count source permutation, and separately shift the value-inference
batch boundary): the raw child is read exactly ONCE. Masking and FK-key
staging fuse into that one pass; FK resolution runs later, from an artifact of
that SAME read, never from the source again.

1. Phase 1 -- the ONLY raw source pass. Per raw batch: stage every incoming
   edge's `(row_nr, join_key, src)` keys into that edge's `SpillChildKeys`
   Arrow-IPC file (`StreamFkJoiner.stage_batch`), mask the non-FK columns
   (`mask_batch`), and append the masked batch to the **payload store**
   (`ResidentPayloadStore` in memory when there is no sink, `SpillPayloadStore`
   when there is).
2. Phase 2 -- one bounded-reorder join per edge, in edge order, so at most one
   DuckDB connection is ever live: for edge k, (a) if its orphan policy is
   FAIL, run `total_orphans()` and raise if non-zero; (b) call
   `run_ordered_join(...)`, which drains the unordered join into a
   `BoundedExternalSorter` and closes the joiner's DuckDB connection before
   returning; (c) wrap the result in a `JoinRowCursor` and register it with the
   `ExitStack` immediately; (d) advance to edge k+1. Every FAIL precount still
   completes before phase 3 emits any output, because phase 3 runs only after
   this whole loop.
3. Phase 3 -- resolve from the payload store, no source read. For each masked
   payload batch, `cursor.take(m, row_nr_start)` pulls exactly that batch's
   raw join rows (asserting their row_nr against the payload's OWN row_nr --
   two artifacts of the same read), then `joiner.resolve_batch` produces that
   batch's FK output, which the FK columns overwrite before the batch is
   emitted.

The `ExitStack` opened for phase 2 wraps phase 2 AND the whole of phase 3: it
holds every edge's `_OrderedJoinRows` open across phase 3 (each payload batch
draws from every edge's cursor), and closes all of them -- on normal
completion or on abandonment/exception -- in one place. `run_ordered_join`
itself already closes its joiner's connection before returning, so only one
join connection is ever live at a time; the sorters' bounded merge readers
stay open (and bounded by `run_bytes_cap` each) through phase 3.

Every heavy relational operation (the join, its spill, the sort) is delegated
to DuckDB / `BoundedExternalSorter`; nothing here re-implements memory
management. Byte-parity to the pandas oracle is unchanged and pinned by
`tests/parity/`.
"""

from __future__ import annotations

import shutil
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.compute as pc

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
    reconcile_batch,
)
from decoy_engine.execution.out_of_core._join import orphan_fk_error, orphan_fk_warning
from decoy_engine.execution.out_of_core._mask import mask_batch
from decoy_engine.execution.out_of_core._memory_estimate import resolve_phase_memory_limits
from decoy_engine.execution.out_of_core._payload_store import (
    PayloadStore,
    RawParentKeySpill,
    ResidentPayloadStore,
    SpillPayloadStore,
)
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_aligned
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_driver_support import (
    _code_set_records_and_evidence_for_table,
    _fixed_output_schema,
    _fk_component_map,
    _payload_schema,
    _replace_fk_columns,
)
from decoy_engine.execution.out_of_core._stream_join import JoinRowCursor, StreamFkJoiner
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import TypeAlias

    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation, ParentSource
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.relationships._graph import RelationshipEdge

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
    run_bytes_cap: int,
    sink: TransactionalSink | None,
    outputs: dict[str, pa.Table],
    warnings: list[QualityWarning],
    merge_fan_in: int = 16,
    budget_bytes: int | None = None,
    unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
    mask_key: bytes | None = None,
    code_set_corpora: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> None:
    """Rewrite one table via the three-phase sequential reorder driver.

    Emits either into `sink.write_batches` (bounded residency, fixed schema) or
    into `outputs` (in-memory reassembly with whole-column type semantics).
    Each outgoing parent-key relation is built from the table's final output
    and stored into `parent_relations` for later children. WARN orphan totals
    are aggregated over the whole stream and appended to `warnings` in
    incoming-edge order. `budget_bytes` (when not None) sizes the joiner/build
    DuckDB connections by their own phase-local liveness (see
    `_memory_estimate.resolve_phase_memory_limits`).

    `run_bytes_cap` / `merge_fan_in` bound each edge's `BoundedExternalSorter`
    (phase 2's order-restore sort), threaded straight into every
    `run_ordered_join` call; they are plain per-run budgets, not
    `ReorderBudgets` (deriving those from the process ceiling + disk ledger,
    and choosing this route over `_batch_join`, is the route-seam's job, not
    this standalone driver's).

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
    # masked-output order), never a second read of `raw` itself (a same-count
    # on-disk permutation of `raw` after phase 1 could silently misalign
    # positionally against the masked output). `raw` is a harmless default
    # when there are no outgoing edges, since the relation build then never
    # runs.
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
        # Finalize the spill now: phase 1 is done reading `raw`, and the
        # outgoing-relation build (below, or in `emit_to_sink`) reads this
        # stream back via `raw_parent_source` -- it must carry its end-of-stream
        # marker before any reader opens it.
        if raw_parent_spill is not None:
            raw_parent_spill.finalize()
        # Same reasoning, per edge: each joiner's child-key spill must carry
        # its end-of-stream marker before total_orphans/run_ordered_join
        # (phase 2) opens the first reader over it.
        for joiner in joiners:
            joiner.finalize_staging()

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

        # --- Phase 2 + 3, one ExitStack over both. Each edge, in order: the
        # FAIL precount (if any), then the bounded-reorder join (which closes
        # its own DuckDB connection before returning -- at most one join
        # connection is ever live), then the resulting `_OrderedJoinRows` is
        # registered with the stack immediately. Phase 3 (below, inside the
        # same `with`) draws from every edge's cursor per payload batch, so
        # every cursor must stay open across the whole of phase 3; the stack
        # closes all of them on normal completion or on abandonment/exception. ---
        with ExitStack() as stack:
            cursors: list[JoinRowCursor] = []
            for edge, joiner in zip(incoming_edges, joiners, strict=True):
                if edge.orphan_policy is OrphanPolicy.FAIL:
                    total = joiner.total_orphans()
                    if total:
                        raise orphan_fk_error(edge, total)
                rows = joiner.run_ordered_join(
                    batch_rows, run_bytes_cap=run_bytes_cap, merge_fan_in=merge_fan_in
                )
                stack.enter_context(rows)
                cursors.append(JoinRowCursor(rows, join_columns=edge.child_columns))

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
        # Deferred commit: `code_set_null_seen` was populated during the phase-1
        # source pass (which always precedes this point), so it reflects the
        # whole stream. Only a column that saw a non-missing value earns its
        # stamp (masked_any parity).
        if code_set_corpora is not None:
            for key, evidence in table_code_set_evidence.items():
                if code_set_null_seen.get(key[1], False):
                    code_set_corpora[key] = evidence
        for edge, joiner in zip(incoming_edges, joiners, strict=True):
            if joiner.orphan_total and edge.orphan_policy is OrphanPolicy.WARN:
                warnings.append(orphan_fk_warning(edge, joiner.orphan_total))
    finally:
        # Best-effort cleanup: every resource release below is INDEPENDENTLY
        # guarded so a failure in one (e.g. a `finalize_staging` that raises on
        # an error path) still runs the rest -- every joiner is closed, the
        # payload store is closed, and `temp_dir` is removed. The guards swallow
        # cleanup-only errors so the finally never raises, preserving the primary
        # exception from the try body; the driver's output is already delivered
        # to the sink in phase 3, so a post-hoc release error changes no output.
        # The `finalize_staging` re-run is guard-only (idempotent, mirrors the
        # raw_parent_spill re-finalize): it catches a mid-phase-1 abort where the
        # normal finalize was never reached, but must never gate `close()`.
        for joiner in joiners:
            try:
                joiner.finalize_staging()
            except Exception:  # guard-only re-finalize; must never gate close()
                pass
            try:
                joiner.close()
            except Exception:  # best-effort release; every joiner must be closed
                pass
        try:
            store.close()
        except Exception:  # best-effort release
            pass
        if raw_parent_spill is not None:
            try:
                raw_parent_spill.finalize()
            except Exception:  # guard-only re-finalize
                pass
        # Every spill this call created -- child_keys.arrow per edge,
        # raw_parent_keys.arrow, payload.arrow, each edge's reorder run files --
        # lives under this table's OWN `temp_dir` and has had its last read by
        # this point (success or failure via the ExitStack above), so there is
        # no reason to leave it on disk. Best-effort: a table this call never
        # reached (an exception before `temp_dir` was even created) leaves
        # nothing to remove.
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


def _column_seed(plan: Plan, table: str, column: str) -> ColumnSeed | None:
    for table_name, candidate_seed in plan.seed_envelope.per_table:
        if table_name != table:
            continue
        for col_name, col_seed in candidate_seed.per_column:
            if col_name == column:
                return col_seed
    return None


__all__ = ["stream_table"]

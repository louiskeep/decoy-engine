"""Per-table rewrite pass of the out-of-core FK route, split out of ``_runner``.

``_runner`` orchestrates the topological table loop and the run's resources; this module owns one
table's pass: open a joiner per incoming edge, stream the raw batches through ``mask_batch`` and
the joiners under one fixed output schema, then emit into the sink or reassemble resident, and
publish the table's outgoing parent-key relations.

``mask_batch`` and ``_iter_source_batches`` are resolved through ``_runner`` at call time on
purpose: ``_runner.mask_batch`` and ``_runner._iter_source_batches`` are patch targets that the
parity and unit suites rely on, and they must stay effective for the code that moved here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine import reasons
from decoy_engine.declared_casting import _stream as _declared
from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_resolve import orphan_fk_violation_error
from decoy_engine.execution._output_projection import (
    UnconfiguredColumnPolicy,
    enforce_output_projection,
)
from decoy_engine.execution._transactional_sink import TransactionalSink
from decoy_engine.execution.out_of_core import _join as _join_ooc
from decoy_engine.execution.out_of_core import _mask_group_c, _runner, _validate_batch
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._emit import (
    assemble_resident,
    emit_to_sink,
    empty_output_table,
)
from decoy_engine.execution.out_of_core._hash_run import OocHashRun
from decoy_engine.execution.out_of_core._join import orphan_fk_warning
from decoy_engine.execution.out_of_core._mask import masked_output_type, table_seed
from decoy_engine.execution.out_of_core._memory_estimate import resolve_phase_memory_limits
from decoy_engine.execution.out_of_core._relation import (
    _column_seed,
    build_parent_key_relation_aligned,
)
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_driver_support import (
    _code_set_records_and_evidence_for_table,
)
from decoy_engine.execution.out_of_core._vault_collect import collect_batch_vault
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.progress import get_active_channel
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import TypeAlias

    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships._graph import RelationshipEdge

    TableSource: TypeAlias = pa.Table | LazySource


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
    budget_bytes: int | None,
    sink: TransactionalSink | None,
    outputs: dict[str, pa.Table],
    warnings: list[QualityWarning],
    unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
    mask_key: bytes | None = None,
    code_set_corpora: dict[tuple[str, str], dict[str, Any]] | None = None,
    hash_run: OocHashRun | None = None,
    vault_writer: Any = None,
    vault_config: Mapping[str, Any] | None = None,
    validators: Any = None,
    cast_run: Any = None,
) -> None:
    """Rewrite pass for one table: mask + join per batch, then emit.

    Emits either into `sink.write_batches` (bounded residency, fixed schema)
    or into `outputs` (in-memory reassembly with whole-column type semantics).
    Each outgoing parent-key relation is then built from the table's final
    output (staged narrow copy or resident table) and stored into
    `parent_relations` for the children later in the topo order. WARN orphan
    totals are aggregated over the whole stream and appended to `warnings` in
    incoming-edge order, matching whole-table reporting. `budget_bytes`
    (when not None) sizes the joiner/build DuckDB connections by their own
    phase-local liveness rather than one flat `memory_limit` (see
    `_memory_estimate.resolve_phase_memory_limits`).

    `code_set_corpora` (default None): the shared corpus-provenance evidence
    sink, mutated in place like `outputs`/`warnings`. Each column's evidence
    commits only once its stream has seen a value the mask kernel would
    dispatch -- missing (null OR float NaN, per the kernel's `_is_missing`)
    never counts -- so an all-null (or all-NaN-float) column reports no corpus
    as "used", matching `CodeSetHandler.run`'s masked_any gate. The corpus
    record is resolved ONCE here and threaded into every `mask_batch` call AND
    the evidence stamp, so a corpus swapped mid-stream cannot make batches or
    the stamp disagree on version.
    """
    if batch_rows is None:
        batch_rows = _join_ooc._JOIN_BATCH_ROWS
    source_schema = raw.schema
    skip_columns = frozenset(col for edge in incoming_edges for col in edge.child_columns)
    # Hash columns are decided once here, before any row is read (a column never changes backend).
    hash_plan = (hash_run or OocHashRun()).prepare_table(
        plan, table_name, source_schema, skip_columns, incoming_edges, site="batch_join"
    )
    code_set_corpus_records, table_code_set_evidence = _code_set_records_and_evidence_for_table(
        plan, table_name, source_schema.names, skip_columns=skip_columns
    )
    # Evidence is resolved from plan+schema before any row is read; the commit
    # is deferred until the stream has seen a non-missing value per column.
    code_set_null_seen: dict[str, bool] = dict.fromkeys(code_set_corpus_records, False)
    # Round-2 MEDIUM-1: one accumulator per table, reused across every
    # `mask_batch` call below so a multi-batch stream aggregates into ONE
    # `{detector_id: count}` dict per text_mask column; the aggregate
    # `text_mask_sub_floor_span_handled` QualityWarning is emitted once the
    # whole stream is consumed, matching the full-frame handler's per-column
    # aggregate (never per-batch, which would fragment one column's notice
    # across however many batches the stream happened to use).
    text_mask_sub_floor_totals: dict[str, dict[str, int]] = {}
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
                    joiner_memory_limit,
                    mask_key=mask_key,
                )
            )
        for edge, joiner in zip(incoming_edges, joiners, strict=True):
            if edge.orphan_policy is OrphanPolicy.FAIL:
                _raise_on_total_orphans(edge, joiner, raw, batch_rows)
        fk_components = _fk_component_map(incoming_edges, joiners)
        fixed_schema = _fixed_output_schema(plan, table_name, source_schema, fk_components)
        if cast_run is not None:  # F06: declared formatted columns leave as text or bytes
            fixed_schema = cast_run.rendered_schema(table_name, fixed_schema)
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
            for raw_batch in _runner._iter_source_batches(raw, batch_rows):
                get_active_channel().rows(table_name, raw_batch.num_rows)
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
                out = _runner.mask_batch(
                    plan,
                    table_name,
                    raw_batch,
                    skip_columns=skip_columns,
                    mask_key=mask_key,
                    code_set_corpus_records=code_set_corpus_records,
                    sub_floor_totals=text_mask_sub_floor_totals,
                    hash_plan=hash_plan,
                )
                # Before the join: FK child keys are not yet remapped here (they decline admission).
                collect_batch_vault(vault_config, table_name, raw_batch, out, vault_writer)
                _validate_batch.collect_batch_validation(validators, table_name, out)
                for join_idx, joiner in enumerate(joiners):
                    # key_source pins every join to the immutable raw batch:
                    # edges overlapping on a child column must each key off
                    # the source values, not an earlier edge's rewrite.
                    out, orphans = joiner.join_batch(out, key_source=raw_batch)
                    orphan_totals[join_idx] += orphans
                yield out

        def _release_joiners() -> None:
            # Free joiner DuckDB instances before the relation build (emit_to_sink).
            for joiner in joiners:
                joiner.close()

        if sink is not None:
            emit_to_sink(
                plan,
                table_name,
                raw,
                _declared.maybe_render(cast_run, table_name, rewritten()),
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
            batches = list(_declared.maybe_render(cast_run, table_name, rewritten()))
            table_out = (
                assemble_resident(batches, fk_components)
                if batches
                else _declared.maybe_render_empty(
                    cast_run,
                    table_name,
                    empty_output_table(plan, table_name, source_schema, fk_components),
                )
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
        # `code_set_null_seen` reflects the whole stream. Only a column that
        # saw a non-missing value earns its stamp (masked_any parity).
        if code_set_corpora is not None:
            for key, evidence in table_code_set_evidence.items():
                if code_set_null_seen.get(key[1], False):
                    code_set_corpora[key] = evidence
        for edge, total in zip(incoming_edges, orphan_totals, strict=True):
            if total and edge.orphan_policy is OrphanPolicy.WARN:
                warnings.append(orphan_fk_warning(edge, total))
        # Round-2 MEDIUM-1: same deferred-commit reasoning as the code_set
        # block above -- the whole stream has been consumed by now, so
        # `text_mask_sub_floor_totals` holds the table's true aggregate.
        # `table_seed` re-resolves the per-column config to read each
        # column's `sub_floor_span` policy for the warning (mask_batch itself
        # already re-resolved the same seed on every batch; this is one more
        # cheap lookup, not a second masking pass).
        if text_mask_sub_floor_totals:
            table_column_seed = table_seed(plan, table_name)
            if table_column_seed is not None:
                for column, column_seed in table_column_seed.per_column:
                    by_detector = text_mask_sub_floor_totals.get(column)
                    if not by_detector:
                        continue
                    cfg = provider_config_to_dict(column_seed.provider_config)
                    policy = cfg.get("sub_floor_span")
                    warnings.append(
                        _mask_group_c.text_mask_sub_floor_warning(
                            column, str(policy) if policy is not None else None, by_detector
                        )
                    )
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
    for raw_batch in _runner._iter_source_batches(raw, batch_rows):
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


__all__ = ["_stream_table"]

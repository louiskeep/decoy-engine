"""Pure/leaf helpers for `_stream_driver.py`, split out to satisfy the module
size sentry (`tests/sentry/test_module_size.py`, 600-LOC hard cap for a new
module). None of this is control flow: schema derivation, a column ->
(joiner, component) lookup, the code_set evidence resolver, and the one-batch
FK-column replace. `_stream_driver.py` owns everything with a lifecycle
(joiners, cursors, the ExitStack); this module owns everything that does not.

Concrete `StreamFkJoiner` typing is confined here and to `_stream_driver.py`
(the reorder driver's own private surface); `_emit.py`/`_stage.py` stay typed
against a structural Protocol because they are shared with the live
`_batch_join` route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution.out_of_core._mask import masked_output_type, table_seed
from decoy_engine.transforms.code_set import (
    CodeSetConfig,
    describe_loaded_corpus,
    resolve_corpus_record,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships._graph import RelationshipEdge
    from decoy_engine.transforms._codeset_loader import _CorpusRecord


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

    Same field semantics as `ChildFkBatchJoiner`'s own `_replace_fk_columns`
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


__all__ = [
    "_code_set_records_and_evidence_for_table",
    "_fixed_output_schema",
    "_fk_component_map",
    "_payload_schema",
    "_replace_fk_columns",
]

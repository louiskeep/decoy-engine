"""Out-of-core FK child join operators.

Same external-memory discipline as the relation build (`_relation.py`): Python
holds one bounded Arrow batch at a time on both sides of DuckDB, and DuckDB
owns the O(rows) relational work (hash LEFT JOIN, anti-join count, ORDER BY)
with on-disk spill. This is the established out-of-core join shape (grace /
hybrid hash join with graceful degradation, as implemented by DuckDB's
larger-than-memory hash join and external merge sort); we do not roll our own
partitioning or spill.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import NULL_FK_KEY, fk_join_key_tuple, fk_key_value
from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation

# Child rows consumed per streamed batch, matching the relation builder's
# _RELATION_BATCH_ROWS: only one batch of keys is resident in Python at a time,
# on the way into DuckDB and on the way back out of the ordered join. The
# streaming runner reads this at call time, so it is the one batch-size knob
# for the whole out-of-core route.
_JOIN_BATCH_ROWS: Final = 65_536


def orphan_fk_error(edge: RelationshipEdge, orphan_count: int) -> ExecutionError:
    """The FAIL-policy error, shared so every join surface reports one shape."""
    return ExecutionError(
        code="orphan_fk_violation",
        message=(
            f"{orphan_count} orphan FK row(s) found for {edge.child_table}.{edge.child_columns}."
        ),
    )


def orphan_fk_warning(edge: RelationshipEdge, orphan_count: int) -> QualityWarning:
    """The WARN-policy warning, shared so every join surface reports one shape."""
    return QualityWarning(
        code="orphan_fk",
        provider=edge.namespace,
        column=",".join(edge.child_columns),
        detail={
            "parent_table": edge.parent_table,
            "parent_columns": list(edge.parent_columns),
            "child_table": edge.child_table,
            "child_columns": list(edge.child_columns),
            "orphan_rows": orphan_count,
        },
    )


def mask_child_fk(
    *,
    child: pa.Table,
    source_child: pa.Table | None = None,
    edge: RelationshipEdge,
    parent_relation: ParentKeyRelation,
    temp_dir: Path,
    remap_values: tuple[pa.Array, ...] | None = None,
    memory_limit: str | None = None,
) -> tuple[pa.Table, tuple[QualityWarning, ...]]:
    """Replace a single child FK column through a parent relation.

    Null child keys preserve as null and are not orphans, matching the pandas
    adapter's current FK behavior. Orphan policies are explicit control flow
    over the streamed left-join result: child keys enter DuckDB as a lazy
    bounded RecordBatchReader and the ordered join comes back batch-wise, so
    join processing holds nothing sized by total child cardinality. The
    resident `child` frame itself (and the runner-precomputed `remap_values`)
    are bounded by later sprints, not here.
    """
    if edge.orphan_policy is OrphanPolicy.REMAP and remap_values is None:
        raise ExecutionError(
            code="out_of_core_remap_values_missing",
            message="orphan_policy='remap' requires precomputed remap values.",
        )
    for child_col in edge.child_columns:
        if child_col not in child.column_names:
            raise ExecutionError(
                code="out_of_core_child_column_missing",
                message=f"child source table has no column {child_col!r}.",
            )

    source_table = source_child if source_child is not None else child
    for child_col in edge.child_columns:
        if child_col not in source_table.column_names:
            raise ExecutionError(
                code="out_of_core_child_column_missing",
                message=f"child source table has no column {child_col!r}.",
            )

    batch_rows = _JOIN_BATCH_ROWS
    reader = pa.RecordBatchReader.from_batches(
        _child_key_schema(source_table, edge.child_columns),
        _child_key_batches(source_table, edge.child_columns, batch_rows),
    )
    orphan_count = 0
    output_chunks: list[list[pa.Array]] = [[] for _ in edge.child_columns]
    conn = connect_duckdb(temp_dir=temp_dir / "duckdb", memory_limit=memory_limit)
    try:
        conn.register("child_keys_stream", reader)
        # The stream is single-pass but the FAIL count and the join are two
        # scans, so DuckDB owns a spillable copy instead of Python holding one.
        conn.execute("CREATE TEMP TABLE child_keys AS SELECT * FROM child_keys_stream")
        conn.execute(
            "CREATE TEMP VIEW parent_keys AS SELECT * FROM "
            f"read_parquet({_sql_string(str(parent_relation.path))})"
        )
        if edge.orphan_policy is OrphanPolicy.FAIL:
            # Streaming anti-join reducer: the total orphan count is the only
            # thing resident, and it raises before any output is produced.
            fail_count = conn.execute(
                f"""
                SELECT count(*)
                FROM child_keys c
                LEFT JOIN parent_keys p
                  ON c.__decoy_fk_join_key = p.{_q(parent_relation.join_key_column)}
                WHERE c.__decoy_fk_join_key IS NOT NULL
                  AND p.{_q(parent_relation.join_key_column)} IS NULL
                """
            ).fetchone()[0]
            if fail_count:
                raise orphan_fk_error(edge, fail_count)
        select_list = [f"c.{_q('__decoy_row_nr')}", f"c.{_q('__decoy_fk_join_key')}"]
        select_list += [f"c.{_q(f'__decoy_src_{idx}')}" for idx in range(len(edge.child_columns))]
        # Explicit LEFT JOIN match indicator: parent_keys only ever holds rows
        # whose join key was non-null (null-key parent rows never enter the
        # relation, see _relation.py), so p.join_key_column is NULL if and
        # only if the join found no parent row. The masked value itself must
        # NOT be used for this (a legitimate mask, e.g. redact, can produce
        # null for a matched parent), or a matched-but-null-masked child would
        # be misclassified as an orphan.
        select_list.append(
            f"p.{_q(parent_relation.join_key_column)} AS {_q('__decoy_parent_match')}"
        )
        for idx, masked_column in enumerate(parent_relation.masked_key_columns):
            select_list.append(f"p.{_q(masked_column)} AS {_q(f'__decoy_parent_masked_{idx}')}")
        query = f"""
            SELECT {", ".join(select_list)}
            FROM child_keys c
            LEFT JOIN parent_keys p
              ON c.__decoy_fk_join_key = p.{_q(parent_relation.join_key_column)}
            ORDER BY c.__decoy_row_nr
        """
        # DuckDB owns the sort with spill; Python sees one result batch at a
        # time, never the whole join as one Arrow table.
        for batch in conn.execute(query).to_arrow_reader(batch_rows):
            orphan_count += _append_output_batch(
                batch,
                edge=edge,
                remap_values=remap_values,
                output_chunks=output_chunks,
            )
    finally:
        conn.close()

    warnings: tuple[QualityWarning, ...] = ()
    if orphan_count and edge.orphan_policy is OrphanPolicy.WARN:
        warnings = (orphan_fk_warning(edge, orphan_count),)

    result = child
    for idx, child_col in enumerate(edge.child_columns):
        child_idx = result.schema.get_field_index(child_col)
        result = result.set_column(child_idx, child_col, _concat_fk_chunks(output_chunks[idx]))
    return result, warnings


def mask_child_fk_fail(
    *,
    child: pa.Table,
    edge: RelationshipEdge,
    parent_relation: ParentKeyRelation,
    temp_dir: Path,
    memory_limit: str | None = None,
) -> pa.Table:
    """Compatibility wrapper for the first fail-only tests."""
    output, _warnings = mask_child_fk(
        child=child,
        source_child=None,
        edge=edge,
        parent_relation=parent_relation,
        temp_dir=temp_dir,
        memory_limit=memory_limit,
    )
    return output


def _child_key_schema(source_table: pa.Table, child_columns: tuple[str, ...]) -> pa.Schema:
    return pa.schema(
        [
            pa.field("__decoy_row_nr", pa.int64()),
            pa.field("__decoy_fk_join_key", pa.string()),
        ]
        + [
            pa.field(f"__decoy_src_{idx}", source_table.column(col).type)
            for idx, col in enumerate(child_columns)
        ]
    )


def _child_key_batches(
    source_table: pa.Table,
    child_columns: tuple[str, ...],
    batch_rows: int,
) -> Iterator[pa.RecordBatch]:
    """Yield bounded (row_nr, join_key, source components) child-key batches.

    Only `batch_rows` source key values are converted to Python at a time (the
    join-key encoding is a per-scalar function); the raw source components ride
    through as sliced Arrow arrays so `preserve` output can be rebuilt per
    result batch. Unlike the relation build, null-key rows are KEPT (null
    join_key) because every child row must come back out of the join.
    """
    schema = _child_key_schema(source_table, child_columns)
    num_rows = source_table.num_rows
    for start in range(0, num_rows, batch_rows):
        length = min(batch_rows, num_rows - start)
        source_slices = [
            source_table.column(col).slice(start, length).combine_chunks() for col in child_columns
        ]
        source_py = [component.to_pylist() for component in source_slices]
        join_keys: list[str | None] = []
        for row in range(length):
            source_key = tuple(component[row] for component in source_py)
            if any(fk_key_value(value) is NULL_FK_KEY for value in source_key):
                join_keys.append(None)
            else:
                join_keys.append(fk_join_key_tuple(source_key))
        yield pa.record_batch(
            [
                pa.array(range(start, start + length), type=pa.int64()),
                pa.array(join_keys, type=pa.string()),
                *source_slices,
            ],
            schema=schema,
        )


def _append_output_batch(
    batch: pa.RecordBatch,
    *,
    edge: RelationshipEdge,
    remap_values: tuple[pa.Array, ...] | None,
    output_chunks: list[list[pa.Array]],
) -> int:
    """Apply the orphan policy to one ordered join-result batch.

    Returns this batch's orphan count; every Python list here is bounded by
    the result batch size, never by total child cardinality.
    """
    n_components = len(edge.child_columns)
    row_nrs = batch.column("__decoy_row_nr").to_pylist()
    join_keys = batch.column("__decoy_fk_join_key").to_pylist()
    source_components = [
        batch.column(f"__decoy_src_{idx}").to_pylist() for idx in range(n_components)
    ]
    masked_components = [
        batch.column(f"__decoy_parent_masked_{idx}").to_pylist() for idx in range(n_components)
    ]
    # Whether the LEFT JOIN found a parent row, NOT whether the masked value
    # is null: a parent key that legitimately masks to null (e.g. redact
    # producing null, or an upstream FK rewrite yielding null) is still a
    # match, and must resolve to that masked (null) value rather than being
    # treated as an orphan.
    matched = batch.column("__decoy_parent_match").to_pylist()
    orphans = 0
    out: list[list[object]] = [[] for _ in range(n_components)]
    for row, join_key in enumerate(join_keys):
        if join_key is None:
            for component in out:
                component.append(None)
        elif matched[row] is not None:
            for idx, component in enumerate(out):
                component.append(masked_components[idx][row])
        else:
            orphans += 1
            if edge.orphan_policy is OrphanPolicy.REMAP:
                if remap_values is None:
                    raise AssertionError("remap values checked before join")
                # Remap arrays are runner-precomputed over the WHOLE child, so
                # index by the global row number, not the batch offset.
                for idx, component in enumerate(out):
                    component.append(remap_values[idx][row_nrs[row]].as_py())
            else:
                # PRESERVE/WARN keep the source key, but normalized the same way
                # the pandas oracle's child_keys are (fk_key_value), not the raw
                # value: a whole-number float key must read back as the int the
                # oracle's parent_map lookup already collapsed it to.
                for idx, component in enumerate(out):
                    component.append(fk_key_value(source_components[idx][row]))
    for idx, component in enumerate(out):
        output_chunks[idx].append(pa.array(component, from_pandas=True))
    return orphans


def _concat_fk_chunks(chunks: list[pa.Array]) -> pa.Array:
    """Concatenate per-batch FK output arrays into one column.

    Per-batch pa.array() inference can disagree where whole-column inference
    would promote: an all-null batch infers null, homogeneous int vs float
    batches infer int64 vs float64, matched-hash vs bytes-orphan batches infer
    string vs binary, and per-batch decimals infer differing precision/scale.
    Promotion here is Arrow's own field-merge rule (pa.unify_schemas with
    promote_options="permissive", the promotion pyarrow.dataset uses to
    reconcile fragment schemas), which the oracle battery in
    test_out_of_core_join_chunked.py proves equal to one pa.array() over all
    rows for exactly these promotions: null with any type, int64/uint64 with
    float64 (to float64), string with binary (to binary), and decimal128
    precision/scale widening. Mixes that Arrow cannot merge (string/int,
    bool/int, bytes/int) fail whole-column inference too, and fall through to
    the same pa.concat_arrays incompatibility error. Decimal mixed with
    non-decimal is the one family where Arrow's merge picks a different type
    than whole-column inference (or coerces where it would raise), so it is
    rejected fail closed rather than allowed to drift.
    """
    if not chunks:
        return pa.array([], from_pandas=True)
    types = {chunk.type for chunk in chunks}
    if len(types) > 1:
        target = _unified_chunk_type(types)
        if target is not None:
            chunks = [chunk.cast(target) for chunk in chunks]
    return pa.concat_arrays(chunks)


def _unified_chunk_type(types: set[pa.DataType]) -> pa.DataType | None:
    """Pick the whole-column-inference type for mixed per-batch chunk types.

    Returns None when no promotion is attempted, leaving pa.concat_arrays to
    raise the same incompatibility whole-column pa.array() would raise.
    """
    non_null = {t for t in types if not pa.types.is_null(t)}
    if len(non_null) <= 1:
        return next(iter(non_null), None)
    decimals = sum(1 for t in non_null if pa.types.is_decimal(t))
    if 0 < decimals < len(non_null):
        # Arrow's permissive merge widens decimal+int64 to a fixed-precision
        # decimal128 where whole-column inference instead picks a digit-fitted
        # precision, and coerces decimal+float64 to double where whole-column
        # inference raises. Neither is byte-identical, so reject fail closed: a
        # compatibility rejection beats byte drift.
        raise ExecutionError(
            code="out_of_core_fk_key_dtype_unsupported",
            message=(
                "out-of-core FK output mixes decimal and non-decimal key "
                f"values ({', '.join(sorted(str(t) for t in non_null))}); this "
                "combination has no promotion byte-identical to whole-column "
                "inference and is rejected rather than allowed to drift."
            ),
        )
    try:
        merged = pa.unify_schemas(
            [pa.schema([("v", t)]) for t in sorted(non_null, key=str)],
            promote_options="permissive",
        )
    except pa.ArrowTypeError:
        # Families Arrow refuses to merge also fail whole-column inference;
        # concatenation raising below is the parity behavior, not a defect.
        return None
    return merged.field("v").type


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


__all__ = ["mask_child_fk", "mask_child_fk_fail"]

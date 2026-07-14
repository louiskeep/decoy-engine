"""Build narrow out-of-core parent key relations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeAlias

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import NULL_FK_KEY, fk_join_key_tuple, fk_key_value
from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
from decoy_engine.execution.out_of_core._mask import mask_column, masked_output_type
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.kernel import hash_array

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.relationships._graph import RelationshipEdge

    # A parent source: resident table, path-backed lazy reader, or any bounded
    # batch stream. Streams keep the whole parent off the heap; the staging
    # loop below is the only consumer and re-bounds whatever it is handed.
    ParentSource: TypeAlias = pa.Table | LazySource | Iterable[pa.RecordBatch]

    # Produces the masked key arrays for one source batch, aligned to the rows
    # keep_mask retains. Receives the batch's own key arrays (not table
    # offsets) so a streamed parent can be masked without random access; the
    # global start row still rides along for aligned-table callers. Injected so
    # both entry points share the batch loop without either one pre-masking a
    # whole column.
    MaskedBatchFn = Callable[[int, "list[pa.Array]", pa.BooleanArray], list[pa.Array]]

# Parent rows consumed per streamed batch. Bounds the relation builder's own
# Python/Arrow residency: only one batch of keys is resident at a time, and
# DuckDB owns the O(rows) last-write-wins dedup with on-disk spill. Sized to keep
# per-batch overhead low without a copy sized by total parent cardinality.
_RELATION_BATCH_ROWS: Final = 65_536


@dataclass(frozen=True)
class ParentKeyRelation:
    path: Path
    join_key_column: str = "__decoy_fk_join_key"
    masked_key_columns: tuple[str, ...] = ("__decoy_masked_key",)

    @property
    def masked_key_column(self) -> str:
        return self.masked_key_columns[0]


def build_parent_key_relation(
    *,
    plan: Plan,
    parent: ParentSource,
    edge: RelationshipEdge,
    temp_dir: Path,
    memory_limit: str | None = None,
    batch_rows: int = _RELATION_BATCH_ROWS,
    mask_key: bytes | None = None,
) -> ParentKeyRelation:
    """Materialize a narrow last-write-wins parent source->masked key relation.

    The parent may be a resident table, a `LazySource`, or any iterable of
    RecordBatches: masking runs per streamed batch, never over the whole
    column, so every source shape honors the same residency bound as the
    staging loop it feeds. Each key column is masked under its own plan seed
    (any admitted per-value strategy), so the relation is buildable from the
    raw stream alone; no masked whole table needs to exist first. That also
    bounds where this builder is valid: a key column that an incoming edge
    rewrites carries join-produced values its own seed cannot reproduce, so
    such parents must build from their post-rewrite output
    (`build_parent_key_relation_aligned`) instead.
    """
    schema = _parent_source_schema(parent)
    key_seeds: list[ColumnSeed] = []
    masked_types: list[pa.DataType] = []
    for parent_col in edge.parent_columns:
        seed = _column_seed(plan, edge.parent_table, parent_col)
        if seed is None:
            raise ExecutionError(
                code="out_of_core_parent_seed_missing",
                message=f"parent key {edge.parent_table}.{parent_col} is not in the plan.",
            )
        if seed.strategy == "hash" and seed.namespace is None:
            raise ExecutionError(
                code="out_of_core_parent_strategy_unsupported",
                message="hash parent FK keys must carry a namespace.",
            )
        if schema is not None and parent_col not in schema.names:
            raise ExecutionError(
                code="out_of_core_parent_column_missing",
                message=f"parent source table has no column {parent_col!r}.",
            )
        source_type = schema.field(parent_col).type if schema is not None else None
        masked_types.append(masked_output_type(seed, source_type))
        key_seeds.append(seed)
    # DE-02: the keyed-mask IKM; None falls back to job_seed (byte-identical).
    job_seed = mask_key if mask_key is not None else plan.seed_envelope.job_seed

    def masked_batch_fn(
        start: int, key_arrays: list[pa.Array], keep_mask: pa.BooleanArray
    ) -> list[pa.Array]:
        # Filter-then-mask is byte-identical to pre-mask-then-filter: every
        # admitted kernel is per-value deterministic and null-preserving, and
        # keep_mask only drops null-key rows. Masking the kept slice keeps the
        # per-call work batch-sized.
        masked: list[pa.Array] = []
        for key_array, seed in zip(key_arrays, key_seeds, strict=True):
            kept = key_array.filter(keep_mask)
            if seed.strategy == "hash":
                if seed.namespace is None:
                    raise AssertionError("hash namespace checked before streaming")
                # Dispatched through this module's own hash_array binding so
                # the hash path's per-batch residency stays observable here.
                masked.append(
                    hash_array(
                        kept,
                        seed=job_seed,
                        namespace=seed.namespace,
                        truncate=_hash_truncate(seed),
                    )
                )
            else:
                masked.append(mask_column(kept, seed, job_seed))
        return masked

    return _build_relation(
        source_parent=parent,
        edge=edge,
        temp_dir=temp_dir,
        memory_limit=memory_limit,
        batch_rows=batch_rows,
        masked_batch_fn=masked_batch_fn,
        masked_types=tuple(masked_types),
    )


def build_parent_key_relation_from_tables(
    *,
    source_parent: pa.Table,
    masked_parent: pa.Table,
    edge: RelationshipEdge,
    temp_dir: Path,
    memory_limit: str | None = None,
    batch_rows: int = _RELATION_BATCH_ROWS,
) -> ParentKeyRelation:
    """Materialize source parent key -> final masked parent key."""
    return build_parent_key_relation_aligned(
        source_parent=source_parent,
        masked_parent=masked_parent,
        edge=edge,
        temp_dir=temp_dir,
        memory_limit=memory_limit,
        batch_rows=batch_rows,
    )


def build_parent_key_relation_aligned(
    *,
    source_parent: ParentSource,
    masked_parent: pa.Table | LazySource,
    edge: RelationshipEdge,
    temp_dir: Path,
    masked_types: tuple[pa.DataType, ...] | None = None,
    memory_limit: str | None = None,
    batch_rows: int = _RELATION_BATCH_ROWS,
) -> ParentKeyRelation:
    """Materialize source parent key -> final masked parent key, row-aligned.

    `masked_parent` is the parent's POST-REWRITE output (never a re-mask of
    the raw keys: an incoming edge may have rewritten a key column to a value
    the column's own seed cannot reproduce), row-aligned with `source_parent`.
    A resident table is addressed by global slice; a `LazySource` (the staged
    narrow copy the streaming runner writes) is walked in lockstep with the
    raw side, so neither side is ever whole-column resident. `masked_types`
    overrides the masked side's file/schema types with the data-derived
    whole-column types (each staged slice is cast losslessly onto them),
    keeping the relation's Parquet footer identical to a whole-table build.
    """
    source_schema = _parent_source_schema(source_parent)
    masked_schema = masked_parent.schema
    for parent_col in edge.parent_columns:
        if source_schema is not None and parent_col not in source_schema.names:
            raise ExecutionError(
                code="out_of_core_parent_column_missing",
                message=f"parent source table has no column {parent_col!r}.",
            )
        if parent_col not in masked_schema.names:
            raise ExecutionError(
                code="out_of_core_parent_column_missing",
                message=f"masked parent table has no column {parent_col!r}.",
            )
    if masked_types is None:
        masked_types = tuple(masked_schema.field(col).type for col in edge.parent_columns)

    if isinstance(masked_parent, pa.Table):
        resident_masked = masked_parent

        def masked_batch_fn(
            start: int, key_arrays: list[pa.Array], keep_mask: pa.BooleanArray
        ) -> list[pa.Array]:
            # The masked columns ride through as sliced Arrow arrays, so their
            # exact type is preserved and never round-trips through Python.
            # Row-aligned tables let the global start/length address the
            # masked side.
            length = len(key_arrays[0])
            return [
                _cast_masked_array(
                    resident_masked.column(col).slice(start, length).combine_chunks(), dtype
                ).filter(keep_mask)
                for col, dtype in zip(edge.parent_columns, masked_types, strict=True)
            ]
    else:
        cursor = _AlignedMaskedCursor(masked_parent, edge.parent_columns, masked_types, batch_rows)

        def masked_batch_fn(
            start: int, key_arrays: list[pa.Array], keep_mask: pa.BooleanArray
        ) -> list[pa.Array]:
            arrays = cursor.take(start, len(key_arrays[0]))
            return [array.filter(keep_mask) for array in arrays]

    return _build_relation(
        source_parent=source_parent,
        edge=edge,
        temp_dir=temp_dir,
        memory_limit=memory_limit,
        batch_rows=batch_rows,
        masked_batch_fn=masked_batch_fn,
        masked_types=masked_types,
    )


def _cast_masked_array(array: pa.Array, target: pa.DataType) -> pa.Array:
    """Cast one masked slice onto its data-derived whole-column type.

    Both reachable casts are lossless by construction: all-null values back
    down to the null type, and fixed-schema widenings (whole-value float64)
    back to the narrower type the whole-column data actually derived.
    """
    if array.type.equals(target):
        return array
    if pa.types.is_null(target):
        return pa.nulls(len(array))
    return array.cast(target)


class _AlignedMaskedCursor:
    """Lockstep batch reader over the masked side of a row-aligned pair.

    The relation staging loop walks the raw source in global row order
    (`_parent_key_batches`); the masked side preserves that order and count by
    construction (the rewrite pass is row-stable). A strictly sequential
    cursor with a bounded carry buffer therefore suffices: no random access,
    and nothing resident beyond one producer batch plus the carry.
    """

    def __init__(
        self,
        source: LazySource,
        columns: tuple[str, ...],
        target_types: tuple[pa.DataType, ...],
        batch_rows: int,
    ) -> None:
        self._batches = source.iter_batches(batch_rows)
        self._columns = columns
        self._types = target_types
        self._pending: list[list[pa.Array]] = [[] for _ in columns]
        self._pending_rows = 0
        self._consumed = 0

    def take(self, start: int, length: int) -> list[pa.Array]:
        if start != self._consumed:
            raise AssertionError("aligned masked cursor consumed out of row order")
        while self._pending_rows < length:
            try:
                batch = next(self._batches)
            except StopIteration:
                raise AssertionError("masked side shorter than its raw source") from None
            for idx, col in enumerate(self._columns):
                array = batch.column(batch.schema.get_field_index(col))
                self._pending[idx].append(_cast_masked_array(array, self._types[idx]))
            self._pending_rows += batch.num_rows
        out: list[pa.Array] = []
        for idx in range(len(self._columns)):
            chunks = self._pending[idx]
            merged = pa.concat_arrays(chunks) if len(chunks) > 1 else chunks[0]
            out.append(merged.slice(0, length))
            remainder = merged.slice(length)
            self._pending[idx] = [remainder] if len(remainder) else []
        self._pending_rows -= length
        self._consumed += length
        return out


def _staging_schema(
    masked_types: tuple[pa.DataType, ...],
    masked_columns: tuple[str, ...],
) -> pa.Schema:
    return pa.schema(
        [
            pa.field("__decoy_row_nr", pa.int64()),
            pa.field("__decoy_fk_join_key", pa.string()),
        ]
        + [pa.field(name, dtype) for name, dtype in zip(masked_columns, masked_types, strict=True)]
    )


def _parent_source_schema(parent: ParentSource) -> pa.Schema | None:
    """The schema knowable without reading data, or None for raw iterables.

    Raw batch iterables carry no up-front schema; their columns are validated
    per batch inside `_parent_key_batches` instead, and only strategies whose
    output type is source-independent can be typed for them.
    """
    if isinstance(parent, pa.Table):
        return parent.schema
    if isinstance(parent, LazySource):
        return parent.schema
    return None


def _parent_key_batches(
    parent: ParentSource,
    parent_columns: tuple[str, ...],
    batch_rows: int,
) -> Iterator[tuple[int, list[pa.Array]]]:
    """Yield (global start row, key-column arrays) bounded by batch_rows.

    Tables are sliced in place (zero-copy) exactly as the pre-streaming build
    did; lazy/iterable sources are consumed one batch at a time, and an
    oversized upstream batch is re-sliced so the residency bound is this
    module's, not the producer's. The running offset keeps row numbers global,
    which is what makes the DuckDB last-write-wins dedup order identical to a
    single-shot build.
    """
    if isinstance(parent, pa.Table):
        for start in range(0, parent.num_rows, batch_rows):
            length = min(batch_rows, parent.num_rows - start)
            yield (
                start,
                [
                    parent.column(col).slice(start, length).combine_chunks()
                    for col in parent_columns
                ],
            )
        return
    batches = parent.iter_batches(batch_rows) if isinstance(parent, LazySource) else parent
    offset = 0
    for batch in batches:
        for col in parent_columns:
            if batch.schema.get_field_index(col) < 0:
                raise ExecutionError(
                    code="out_of_core_parent_column_missing",
                    message=f"parent source table has no column {col!r}.",
                )
        for start in range(0, batch.num_rows, batch_rows):
            length = min(batch_rows, batch.num_rows - start)
            yield (
                offset + start,
                [batch.column(col).slice(start, length) for col in parent_columns],
            )
        offset += batch.num_rows


def _relation_staging_batches(
    *,
    source_parent: ParentSource,
    parent_columns: tuple[str, ...],
    masked_columns: tuple[str, ...],
    masked_types: tuple[pa.DataType, ...],
    masked_batch_fn: MaskedBatchFn,
    batch_rows: int,
) -> Iterator[pa.RecordBatch]:
    """Yield bounded (row_nr, join_key, masked...) batches for the DuckDB dedup.

    Only `batch_rows` source keys are converted to Python at a time (the join-key
    encoding is a per-scalar function); the masked arrays come from the caller's
    per-batch callback, aligned to keep_mask, so no whole-column masked copy ever
    exists. Row numbers are global (the batch offset), so DuckDB's last-write-wins
    dedup over the whole stream is identical to the single-shot build.
    """
    schema = _staging_schema(masked_types, masked_columns)
    for start, key_arrays in _parent_key_batches(source_parent, parent_columns, batch_rows):
        length = len(key_arrays[0])
        source_py = [key_array.to_pylist() for key_array in key_arrays]
        keep: list[bool] = []
        join_keys: list[str] = []
        for row in range(length):
            source_key = tuple(component[row] for component in source_py)
            if any(fk_key_value(value) is NULL_FK_KEY for value in source_key):
                keep.append(False)
            else:
                keep.append(True)
                join_keys.append(fk_join_key_tuple(source_key))
        keep_mask = pa.array(keep, type=pa.bool_())
        # Always advance the masked side, even for an all-null-key window: the
        # aligned-cursor callback tracks consumed rows and would desync from the
        # raw side if a fully filtered window skipped it. Resident and raw-stream
        # callbacks return empty arrays under an all-false mask.
        masked_arrays = masked_batch_fn(start, key_arrays, keep_mask)
        if not join_keys:
            continue
        row_nr = pa.array(range(start, start + length), type=pa.int64()).filter(keep_mask)
        yield pa.record_batch(
            [row_nr, pa.array(join_keys, type=pa.string()), *masked_arrays],
            schema=schema,
        )


def _build_relation(
    *,
    source_parent: ParentSource,
    edge: RelationshipEdge,
    temp_dir: Path,
    memory_limit: str | None,
    batch_rows: int,
    masked_batch_fn: MaskedBatchFn,
    masked_types: tuple[pa.DataType, ...],
) -> ParentKeyRelation:
    masked_columns = tuple(
        "__decoy_masked_key" if idx == 0 else f"__decoy_masked_key_{idx}"
        for idx in range(len(edge.parent_columns))
    )
    batches = _relation_staging_batches(
        source_parent=source_parent,
        parent_columns=edge.parent_columns,
        masked_columns=masked_columns,
        masked_types=masked_types,
        masked_batch_fn=masked_batch_fn,
        batch_rows=batch_rows,
    )
    reader = pa.RecordBatchReader.from_batches(
        _staging_schema(masked_types, masked_columns),
        batches,
    )
    temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = temp_dir / (
        f"{edge.parent_table}_{_column_tuple_slug(edge.parent_columns)}_key_relation.parquet"
    )
    conn = connect_duckdb(temp_dir=temp_dir / "duckdb", memory_limit=memory_limit)
    try:
        # DuckDB scans the RecordBatchReader lazily and owns the dedup + spill, so
        # nothing sized by total parent cardinality is ever resident in Python.
        conn.register("parent_keys", reader)
        conn.execute(
            """
            COPY (
                SELECT __decoy_fk_join_key, {masked_select}
                FROM parent_keys
                QUALIFY row_number() OVER (
                    PARTITION BY __decoy_fk_join_key
                    ORDER BY __decoy_row_nr DESC
                ) = 1
            ) TO ? (FORMAT PARQUET)
            """.format(masked_select=", ".join(masked_columns)),
            [str(out_path)],
        )
    finally:
        conn.close()
    return ParentKeyRelation(path=out_path, masked_key_columns=masked_columns)


def _column_tuple_slug(columns: tuple[str, ...]) -> str:
    """A collision-free filename slug for a parent-column tuple.

    A plain ``'_'.join(columns)`` is NOT injective: the tuples ('a_b', 'c')
    and ('a', 'b_c') both render 'a_b_c', so two distinct relations from the
    same parent table (different column tuples that underscore-collide) would
    stage to the same Parquet path and the second build would clobber the
    first. A length-prefixed encoding (the same injective framing
    `fk_join_key_tuple` uses) hashed to a fixed-width hex digest is stable and
    unambiguous for arbitrary column names, including names carrying
    filesystem-unsafe characters.
    """
    encoded = "".join(f"{len(col)}:{col}\x00" for col in columns).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _column_seed(plan: Plan, table: str, column: str) -> ColumnSeed | None:
    for table_name, table_seed in plan.seed_envelope.per_table:
        if table_name != table:
            continue
        for col_name, col_seed in table_seed.per_column:
            if col_name == column:
                return col_seed
    return None


def _hash_truncate(seed: ColumnSeed) -> int | None:
    cfg = dict(seed.provider_config)
    raw = cfg.get("truncate")
    return raw if isinstance(raw, int) and raw > 0 else None


__all__ = [
    "ParentKeyRelation",
    "build_parent_key_relation",
    "build_parent_key_relation_aligned",
    "build_parent_key_relation_from_tables",
]

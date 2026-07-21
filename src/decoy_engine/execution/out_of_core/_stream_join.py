"""Single-streaming-join FK joiner for the batch-streaming sink path (OOC-B).

This replaces `_batch_join.ChildFkBatchJoiner`'s per-child-batch join against a
MATERIALIZED parent TEMP TABLE. That table held one row per distinct parent
key and, being a DuckDB temp table, could not fully evict its
buffer-manager/control state, so on a large parent it pinned an
O(distinct-parent-key) resident structure for the whole child stream -- the
floor that made out-of-core peak memory rise with parent row count.

Instead, this joiner uses the SAME shape the whole-child resident path
(`_join.py::mask_child_fk`) already proves: the parent relation is a
`read_parquet` VIEW (never materialized), the child keys are staged once into a
spillable `child_keys` TEMP TABLE, and ONE `LEFT JOIN child_keys x parent_keys
ORDER BY __decoy_row_nr` per edge is read back through `to_arrow_reader`. The
join build, hash table, and external sort are DuckDB's established
larger-than-memory (grace / hybrid hash join + external merge sort) operations
under `memory_limit`, spilling to `temp_directory`; we delegate all
memory-management, spill, and ordering to DuckDB and do not roll our own. The
one difference from `_join.py` is that this joiner EMITS incrementally (an
ordered FK-output reader the runner zips to its mask stream) rather than
setting columns into a whole resident `pa.Table`.

Two invariants carried over UNCHANGED from `ChildFkBatchJoiner`, because the
sink path writes one Parquet file under one schema fixed before the first
batch:

- Fixed output schema. `output_types` is resolved from schemas alone at
  construction (`_batch_join._resolve_output_types`), fail-closing on any mix
  that cannot be typed byte-identically to whole-column inference, and
  `observed_types` records the pre-cast chunk types so `_emit.py` can replay
  the whole-column narrowing on the resident/relation sides. Neither the
  fixed-schema typing nor the documented divergences it carries change here.
- Per-(output-)batch REMAP minting. Orphan REMAP values are minted from each
  join-output batch's own `__decoy_src_i` keys (the raw child values, which
  ride through the join unchanged, so every edge still keys off the RAW child),
  never precomputed over the whole child -- so no kernel call is sized by total
  child cardinality. Because the streamed join numbers rows GLOBALLY (for the
  `ORDER BY`), each output batch's `__decoy_row_nr` is re-based to positional
  0..n before `_append_output_batch`, which uses row_nr solely as the REMAP
  index; the values are identical to the whole-child mint because the kernels
  are per-value deterministic.

See `_batch_join.py`'s module docstring for the full inventory of documented
typing divergences (all preserved), and `tests/parity/` for the byte-parity
gate against the pandas oracle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import NULL_FK_KEY, fk_key_value
from decoy_engine.execution.out_of_core._batch_join import _cast_chunks, _resolve_output_types
from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
from decoy_engine.execution.out_of_core._join import (
    _append_output_batch,
    _child_key_batches,
    _q,
    _sql_string,
)
from decoy_engine.execution.out_of_core._mask import mask_column
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.plan._types import ColumnSeed
    from decoy_engine.relationships._graph import RelationshipEdge


class StreamFkJoiner:
    """One streamed FK join per edge, emitting an ordered FK-output reader.

    Lifecycle, one DuckDB connection per edge (spillable, no materialized
    parent):

    1. Construct: resolve the fixed `output_types` from schemas (may raise
       fail-closed before any connection is opened), then open the connection
       and register the parent relation as a `read_parquet` VIEW.
    2. `begin_staging()` then `stage_batch(source_batch)` per raw child batch
       (or `stage_keys(iter)` for the whole child at once): materialize the
       `child_keys` TEMP TABLE with GLOBAL row numbers, exactly as
       `_join.py::mask_child_fk` stages its child side.
    3. `total_orphans()`: the FAIL-policy anti-join precount over the whole
       child (the runner raises before any output if it is non-zero).
    4. `iter_output(batch_rows)`: run the single ordered LEFT JOIN and yield
       `(row_nr, FK output columns)` result batches, accumulating `orphan_total`
       and `observed_types`.
    5. `close()`.
    """

    def __init__(
        self,
        *,
        edge: RelationshipEdge,
        parent_relation: ParentKeyRelation,
        child_key_types: tuple[pa.DataType, ...],
        temp_dir: Path,
        memory_limit: str | None = None,
        remap_seeds: tuple[ColumnSeed, ...] | None = None,
        job_seed: bytes | None = None,
    ) -> None:
        if len(child_key_types) != len(edge.child_columns):
            raise ExecutionError(
                code="out_of_core_child_key_types_mismatch",
                message="one child key type is required per FK child column.",
            )
        if edge.orphan_policy is OrphanPolicy.REMAP:
            if remap_seeds is None or job_seed is None:
                raise ExecutionError(
                    code="out_of_core_remap_seeds_missing",
                    message="orphan_policy='remap' requires the parent key seeds and job seed.",
                )
            for parent_col, seed in zip(edge.parent_columns, remap_seeds, strict=True):
                if seed is None:
                    raise ExecutionError(
                        code="out_of_core_parent_seed_missing",
                        message=f"parent key {edge.parent_table}.{parent_col} is not in the plan.",
                    )
        self._edge = edge
        self._relation = parent_relation
        self._remap_seeds = remap_seeds
        self._job_seed = job_seed
        self._child_columns = edge.child_columns
        # The key schema mirrors `_join.py::_child_key_schema` exactly (row_nr,
        # join_key, then one raw src column per child key column), built from the
        # source key types the runner passes so the empty child_keys table can be
        # created before any batch is staged.
        self._key_schema = pa.schema(
            [
                pa.field("__decoy_row_nr", pa.int64()),
                pa.field("__decoy_fk_join_key", pa.string()),
            ]
            + [
                pa.field(f"__decoy_src_{idx}", child_key_types[idx])
                for idx in range(len(edge.child_columns))
            ]
        )
        # The relation's Parquet footer is the authoritative masked-key type
        # (already through the DuckDB COPY round trip the join result takes).
        relation_schema = pq.read_metadata(parent_relation.path).schema.to_arrow_schema()
        masked_types = tuple(
            relation_schema.field(name).type for name in parent_relation.masked_key_columns
        )
        self._output_types = _resolve_output_types(
            edge=edge,
            masked_types=masked_types,
            child_key_types=child_key_types,
            remap_seeds=remap_seeds,
        )
        self._observed_types: tuple[set[pa.DataType], ...] = tuple(
            set() for _ in edge.child_columns
        )
        self._orphan_total = 0
        self._staged_rows = 0
        self._staged = False
        # Typing is settled; only now acquire the connection so a fail-closed
        # rejection never leaks one.
        self._conn = connect_duckdb(temp_dir=temp_dir / "duckdb", memory_limit=memory_limit)
        try:
            # Parent as a VIEW, never a materialized TEMP TABLE: this joiner runs
            # ONE join against it per edge (unlike the removed per-batch joiner
            # that ran hundreds), so DuckDB reads the relation parquet once as a
            # spillable grace-hash build side -- the same shape `_join.py` uses.
            self._conn.execute(
                "CREATE TEMP VIEW parent_keys AS SELECT * FROM "
                f"read_parquet({_sql_string(str(parent_relation.path))})"
            )
        except BaseException:
            self._conn.close()
            self._conn = None
            raise

    @property
    def output_types(self) -> tuple[pa.DataType, ...]:
        """The fixed Arrow type of each FK output column, in edge order."""
        return self._output_types

    @property
    def observed_types(self) -> tuple[set[pa.DataType], ...]:
        """Pre-cast chunk types seen so far, per FK component.

        `_emit.py` replays the whole-column permissive merge over these to
        recover the value-derived narrowing the fixed schema cannot know up
        front (the documented divergence), on the resident and relation sides.
        """
        return self._observed_types

    @property
    def orphan_total(self) -> int:
        """Running orphan total over the output emitted so far.

        Fully populated once `iter_output` is drained; the runner reads it for
        the WARN aggregation, matching whole-table reporting.
        """
        return self._orphan_total

    def begin_staging(self) -> None:
        """Create the empty, typed `child_keys` TEMP TABLE.

        Separate from `stage_batch` so the runner can open one table per edge
        and feed every incoming edge from a SINGLE raw source pass (phase 1),
        rather than re-reading the source once per edge.
        """
        if self._staged:
            raise AssertionError("child_keys already staged")
        empty = self._key_schema.empty_table()
        self._conn.register("child_keys_init", empty)
        try:
            self._conn.execute("CREATE TEMP TABLE child_keys AS SELECT * FROM child_keys_init")
        finally:
            self._conn.unregister("child_keys_init")
        self._staged = True

    def stage_batch(self, source_batch: pa.RecordBatch) -> None:
        """Stage one raw child batch's keys into `child_keys` with global row_nr.

        The `(row_nr, join_key, src_i)` encoding is `_join.py::_child_key_batches`
        verbatim (reused, not reimplemented); only the row numbers are shifted
        by the running global offset so the whole child is numbered positionally
        across batches, exactly as a single whole-child stage would.
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before stage_batch")
        self._check_child_columns(source_batch.schema)
        length = source_batch.num_rows
        if length == 0:
            return
        source_table = pa.Table.from_batches([source_batch])
        for key_batch in _child_key_batches(source_table, self._child_columns, length):
            columns = list(key_batch.columns)
            # pc.* funcs are dynamically generated; stubs miss them.
            columns[0] = pc.add(columns[0], self._staged_rows)  # type: ignore[attr-defined, unused-ignore]
            shifted = pa.record_batch(columns, schema=self._key_schema)
            self._conn.register("child_keys_batch", pa.Table.from_batches([shifted]))
            try:
                self._conn.execute("INSERT INTO child_keys SELECT * FROM child_keys_batch")
            finally:
                self._conn.unregister("child_keys_batch")
        self._staged_rows += length

    def stage_keys(self, source_batches: Iterable[pa.RecordBatch]) -> None:
        """Stage a whole child (convenience for a single-edge caller/tests)."""
        self.begin_staging()
        for batch in source_batches:
            self.stage_batch(batch)

    def total_orphans(self) -> int:
        """Whole-child anti-join orphan count (the FAIL-policy precount).

        Mirrors `_join.py::mask_child_fk`'s FAIL count (`_join.py:129-138`): a
        null child key is never an orphan; a non-null key with no matching
        parent row is. Only the count is resident.
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before total_orphans")
        join_key = self._relation.join_key_column
        count = self._conn.execute(
            f"""
            SELECT count(*)
            FROM child_keys c
            LEFT JOIN parent_keys p
              ON c.__decoy_fk_join_key = p.{_q(join_key)}
            WHERE c.__decoy_fk_join_key IS NOT NULL
              AND p.{_q(join_key)} IS NULL
            """
        ).fetchone()[0]
        return int(count)

    def iter_output(self, batch_rows: int) -> Iterator[pa.RecordBatch]:
        """Yield ordered FK-output batches: `(__decoy_row_nr, <fk columns...>)`.

        Runs the single `LEFT JOIN child_keys x parent_keys ORDER BY
        __decoy_row_nr` and reads the ordered result back through
        `to_arrow_reader(batch_rows)` (DuckDB owns the sort + spill; Python sees
        one result batch at a time). Each batch's FK columns are produced by
        `_append_output_batch` (the shared orphan-policy code) and cast to the
        fixed `output_types`, with per-batch REMAP minting.
        """
        if not self._staged:
            raise AssertionError("begin_staging must run before iter_output")
        edge = self._edge
        n_components = len(edge.child_columns)
        join_key = self._relation.join_key_column
        select_list = [f"c.{_q('__decoy_row_nr')}", f"c.{_q('__decoy_fk_join_key')}"]
        select_list += [f"c.{_q(f'__decoy_src_{idx}')}" for idx in range(n_components)]
        # Explicit LEFT JOIN match indicator (mirrors _join.py): parent_keys only
        # ever holds non-null join keys, so p's join-key column is NULL iff no
        # parent row matched -- using the masked value's nullness would
        # misclassify a matched-but-null-masked parent as an orphan.
        select_list.append(f"p.{_q(join_key)} AS {_q('__decoy_parent_match')}")
        for idx, masked_column in enumerate(self._relation.masked_key_columns):
            select_list.append(f"p.{_q(masked_column)} AS {_q(f'__decoy_parent_masked_{idx}')}")
        query = f"""
            SELECT {", ".join(select_list)}
            FROM child_keys c
            LEFT JOIN parent_keys p
              ON c.__decoy_fk_join_key = p.{_q(join_key)}
            ORDER BY c.__decoy_row_nr
        """
        output_schema = pa.schema(
            [pa.field("__decoy_row_nr", pa.int64())]
            + [pa.field(col, self._output_types[idx]) for idx, col in enumerate(edge.child_columns)]
        )
        for result in self._conn.execute(query).to_arrow_reader(batch_rows):
            row_nr = result.column("__decoy_row_nr")
            remap_values = self._batch_remap_values(result)
            # REMAP indexes remap_values by row_nr; re-base to positional 0..n so
            # the per-batch mint aligns (row_nr is used ONLY as the REMAP index).
            source = self._with_positional_row_nr(result) if remap_values is not None else result
            output_chunks: list[list[pa.Array]] = [[] for _ in range(n_components)]
            self._orphan_total += _append_output_batch(
                source,
                edge=edge,
                remap_values=remap_values,
                output_chunks=output_chunks,
            )
            for idx in range(n_components):
                for chunk in output_chunks[idx]:
                    self._observed_types[idx].add(chunk.type)
            fk_arrays = [
                _cast_chunks(output_chunks[idx], self._output_types[idx])
                for idx in range(n_components)
            ]
            yield pa.record_batch([row_nr, *fk_arrays], schema=output_schema)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> StreamFkJoiner:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _check_child_columns(self, schema: pa.Schema) -> None:
        for child_col in self._child_columns:
            if schema.get_field_index(child_col) < 0:
                raise ExecutionError(
                    code="out_of_core_child_column_missing",
                    message=f"child source table has no column {child_col!r}.",
                )

    def _batch_remap_values(self, result: pa.RecordBatch) -> tuple[pa.Array, ...] | None:
        if self._edge.orphan_policy is not OrphanPolicy.REMAP:
            return None
        if self._remap_seeds is None or self._job_seed is None:
            raise AssertionError("remap seeds checked at construction")
        # Mint over ALL of the batch's keys (the kernels are per-value
        # deterministic, so orphan positions carry the same values either way),
        # from the raw src columns that rode through the join unchanged. Bounded
        # by the result batch size, never child cardinality.
        remapped = []
        for idx, seed in enumerate(self._remap_seeds):
            normalized = [
                None if fk_key_value(value) is NULL_FK_KEY else fk_key_value(value)
                for value in result.column(f"__decoy_src_{idx}").to_pylist()
            ]
            remapped.append(
                mask_column(pa.array(normalized, from_pandas=True), seed, self._job_seed)
            )
        return tuple(remapped)

    def _with_positional_row_nr(self, result: pa.RecordBatch) -> pa.RecordBatch:
        columns = list(result.columns)
        idx = result.schema.get_field_index("__decoy_row_nr")
        columns[idx] = pa.array(range(result.num_rows), type=pa.int64())
        return pa.record_batch(columns, schema=result.schema)


__all__ = ["StreamFkJoiner"]

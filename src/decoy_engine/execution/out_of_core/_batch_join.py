"""Per-batch child FK join with a schema-fixed output type.

The whole-child join (`_join.py`) derives each FK output type from the values:
per-result-batch `pa.array` inference reconciled after the fact with Arrow's
permissive schema merge (`_concat_fk_chunks`). A streaming runner cannot do
that, because it writes output batches to one Parquet writer whose schema is
fixed by the first batch (an Arrow `ParquetWriter` writes a single-schema
file; that constraint is the Parquet format's, not ours). So this module
resolves ONE output Arrow type per FK component up front, from schemas alone:
the parent relation's masked-key type plus the orphan policy's contribution.
The only multi-type candidate mix that merges is int64 with float64 (the
permissive promotion the whole-child path pins for that split); every other
mix is rejected up front, because a permissive merge matches whole-column
inference only when the data actually mixes the candidates. Every per-batch
FK output array is cast to the fixed type, so concatenating the per-batch
outputs reproduces the whole-child output whenever the data exercises the
merged type.

Fail-closed rule: where no schema-derived type can be guaranteed
byte-identical to whole-column inference, construction raises
`out_of_core_fk_key_dtype_unsupported` before any output exists. That covers
the mixes the whole-child path already rejects or crashes on (decimal mixed
with non-decimal, string+numeric), every type whose Python-round-trip
inference is value-dependent (decimals digit-fit their precision, uint64
straddles int64), and every promotable multi-type mix outside {int64,
float64}: string with binary, for example, is rejected rather than merged to
binary, because a run whose data stays all-string would then emit binary
scalars where the whole-child path emits strings. A compatibility rejection
beats byte drift.

Known divergences (documented; the first two are pinned in tests):
- Narrowing: when a merged numeric type is float64 but every value a run
  actually emits is integral, the whole-child path narrows to int64 while the
  fixed schema cannot know that. The streaming caller must gate or accept
  such configs explicitly.
- All-null or empty FK child column: per-batch inference leaves the
  whole-child output null-typed, while the joiner emits the fixed type with
  nulls. Values are identical; the fixed type is the writable schema a
  streaming sink needs.
- Composite FK child masked as independent SCALAR seeds (SC2 CF2, GATED): the
  one composite shape that diverges from the pandas oracle. When a composite FK
  edge's child columns carry their own scalar strategies (rather than one
  composite_fk_group over the key), the oracle scalar-masks each column BEFORE
  resolving the FK (FK children resolve last), so a PRESERVE/WARN orphan -- and
  a partial-null key row -- keeps the scalar-MASKED value, while the out-of-core
  route joins on and preserves the RAW source key (a raw-value leak; nulls a
  partial-null key). The compat gate now rejects this shape fail-closed with
  `out_of_core_composite_fk_scalar_child_unsupported` (the job falls back to
  full-frame). The canonical composite_fk_group shape (a single GroupSeed over
  the FK columns) is oracle-parity across orphans, partial-nulls, and every
  policy -- both routes treat a partial-null composite key as fully null -- and
  stays admitted. Pinned in tests/parity/test_out_of_core_fk_parity.py
  (`test_composite_fk_group_orphan_and_partial_null_parity`,
  `test_composite_fk_scalar_child_gate_rejected`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import NULL_FK_KEY, fk_key_value
from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
from decoy_engine.execution.out_of_core._join import (
    _append_output_batch,
    _child_key_batches,
    _child_key_schema,
    _q,
    _sql_string,
    _unified_chunk_type,
    cast_fk_chunk,
    orphan_fk_error,
)
from decoy_engine.execution.out_of_core._mask import mask_column, masked_output_type
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from pathlib import Path

    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.plan._types import ColumnSeed
    from decoy_engine.relationships._graph import RelationshipEdge


class ChildFkBatchJoiner:
    """Joins one child batch at a time against a parent key relation.

    Open once per FK edge (one DuckDB connection, the parent relation
    registered once), then call `join_batch` per input batch. Each call
    returns the batch with its FK columns replaced per the orphan policy and
    the batch's orphan count, which the caller aggregates (WARN total, FAIL
    reporting). `output_types` is fixed at construction, so a caller can build
    its Parquet writer schema before the first batch arrives.
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
        # The relation's Parquet footer is metadata-only to read and is the
        # authoritative masked-key type: it already reflects the DuckDB COPY
        # round trip the join result will take.
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
        # Typing is settled; only now acquire the connection so a fail-closed
        # rejection never leaks one.
        self._conn = connect_duckdb(temp_dir=temp_dir / "duckdb", memory_limit=memory_limit)
        try:
            self._conn.execute(
                "CREATE TEMP VIEW parent_keys AS SELECT * FROM "
                f"read_parquet({_sql_string(str(parent_relation.path))})"
            )
        except BaseException:
            # A constructor that raises never returns, so __exit__/close can
            # never release the connection; release it on the way out.
            self._conn.close()
            self._conn = None
            raise

    @property
    def output_types(self) -> tuple[pa.DataType, ...]:
        """The fixed Arrow type of each FK output column, in edge order."""
        return self._output_types

    @property
    def observed_types(self) -> tuple[set[pa.DataType], ...]:
        """Pre-cast chunk types actually seen so far, per FK component.

        The whole-child path derives its final column type from these chunk
        types (permissive merge, `_concat_fk_chunks`); a caller that holds the
        streamed output in memory can replay that merge to recover the
        value-derived type the fixed schema cannot know up front (the
        documented narrowing divergence).
        """
        return self._observed_types

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> ChildFkBatchJoiner:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def join_batch(
        self,
        batch: pa.RecordBatch,
        *,
        key_source: pa.RecordBatch | None = None,
    ) -> tuple[pa.RecordBatch, int]:
        """Replace one batch's FK columns through the parent relation.

        Returns the rewritten batch and the batch's orphan count. FAIL raises
        on the batch's anti-join count before any output for the batch is
        built; PRESERVE/WARN/REMAP orphan handling reuses the whole-child
        policy code (`_append_output_batch`) verbatim, with REMAP values
        minted from this batch's own keys so no kernel call is sized by total
        child cardinality.

        `key_source` (default: `batch`) supplies every key read - join keys,
        orphan detection, REMAP minting - while the FK columns are written
        into `batch`. A caller threading one batch through several joiners
        passes the immutable raw batch here, so each edge keys off the source
        values (the whole-child contract: every edge joins the RAW child) no
        matter what an earlier edge already rewrote in `batch`.
        """
        edge = self._edge
        keys = batch if key_source is None else key_source
        self._check_child_columns(keys)
        if keys is not batch:
            self._check_child_columns(batch)
            if keys.num_rows != batch.num_rows:
                raise AssertionError("key_source must be row-aligned with the target batch")
        if batch.num_rows == 0:
            empty = [pa.array([], type=dtype) for dtype in self._output_types]
            return self._replace_fk_columns(batch, empty), 0

        remap_values = self._batch_remap_values(keys)
        n_components = len(edge.child_columns)
        output_chunks: list[list[pa.Array]] = [[] for _ in range(n_components)]
        orphan_count = 0
        self._conn.register("child_batch_keys", self._staging_table(keys))
        try:
            if edge.orphan_policy is OrphanPolicy.FAIL:
                self._raise_on_batch_orphans()
            select_list = [f"c.{_q('__decoy_row_nr')}", f"c.{_q('__decoy_fk_join_key')}"]
            select_list += [f"c.{_q(f'__decoy_src_{idx}')}" for idx in range(n_components)]
            # Explicit LEFT JOIN match indicator, mirroring _join.py: the
            # relation only ever holds non-null join keys, so p's join-key
            # column is NULL iff no parent row matched. Using the masked
            # value's nullness instead would misclassify a matched parent
            # whose key legitimately masks to null as an orphan.
            select_list.append(
                f"p.{_q(self._relation.join_key_column)} AS {_q('__decoy_parent_match')}"
            )
            for idx, masked_column in enumerate(self._relation.masked_key_columns):
                select_list.append(f"p.{_q(masked_column)} AS {_q(f'__decoy_parent_masked_{idx}')}")
            query = f"""
                SELECT {", ".join(select_list)}
                FROM child_batch_keys c
                LEFT JOIN parent_keys p
                  ON c.__decoy_fk_join_key = p.{_q(self._relation.join_key_column)}
                ORDER BY c.__decoy_row_nr
            """
            for result in self._conn.execute(query).to_arrow_reader(batch.num_rows):
                orphan_count += _append_output_batch(
                    result,
                    edge=edge,
                    remap_values=remap_values,
                    output_chunks=output_chunks,
                )
        finally:
            self._conn.unregister("child_batch_keys")
        for idx in range(n_components):
            for chunk in output_chunks[idx]:
                self._observed_types[idx].add(chunk.type)
        fk_arrays = [
            _cast_chunks(output_chunks[idx], self._output_types[idx]) for idx in range(n_components)
        ]
        return self._replace_fk_columns(batch, fk_arrays), orphan_count

    def count_batch_orphans(self, batch: pa.RecordBatch) -> int:
        """Anti-join orphan count for one batch, producing no output.

        Lets a caller aggregate a whole-child orphan total in a dedicated raw
        pass (the FAIL policy reports the total, while the fail-fast inside
        `join_batch` only ever sees the first offending batch).
        """
        self._check_child_columns(batch)
        if batch.num_rows == 0:
            return 0
        self._conn.register("child_batch_keys", self._staging_table(batch))
        try:
            return self._batch_orphan_count()
        finally:
            self._conn.unregister("child_batch_keys")

    def _check_child_columns(self, batch: pa.RecordBatch) -> None:
        for child_col in self._edge.child_columns:
            if batch.schema.get_field_index(child_col) < 0:
                raise ExecutionError(
                    code="out_of_core_child_column_missing",
                    message=f"child source table has no column {child_col!r}.",
                )

    def _staging_table(self, batch: pa.RecordBatch) -> pa.Table:
        key_table = pa.Table.from_batches([batch])
        return pa.Table.from_batches(
            list(_child_key_batches(key_table, self._edge.child_columns, batch.num_rows)),
            schema=_child_key_schema(key_table, self._edge.child_columns),
        )

    def _batch_orphan_count(self) -> int:
        count = self._conn.execute(
            f"""
            SELECT count(*)
            FROM child_batch_keys c
            LEFT JOIN parent_keys p
              ON c.__decoy_fk_join_key = p.{_q(self._relation.join_key_column)}
            WHERE c.__decoy_fk_join_key IS NOT NULL
              AND p.{_q(self._relation.join_key_column)} IS NULL
            """
        ).fetchone()[0]
        return int(count)

    def _raise_on_batch_orphans(self) -> None:
        # Per-batch fail-fast reducer: nothing is emitted for a batch that
        # holds an orphan, so a transactional sink never stages doomed output.
        fail_count = self._batch_orphan_count()
        if fail_count:
            raise orphan_fk_error(self._edge, fail_count)

    def _batch_remap_values(self, batch: pa.RecordBatch) -> tuple[pa.Array, ...] | None:
        if self._edge.orphan_policy is not OrphanPolicy.REMAP:
            return None
        if self._remap_seeds is None or self._job_seed is None:
            raise AssertionError("remap seeds checked at construction")
        # Mint over ALL of the batch's keys, not just its orphans: the kernels
        # are per-value deterministic, so the orphan positions carry the same
        # values either way, and `_append_output_batch` can then index by row
        # number unchanged (rows are batch-local here, the staging table is
        # numbered from zero). Bounded by batch size, never child cardinality.
        remapped = []
        for child_col, seed in zip(self._edge.child_columns, self._remap_seeds, strict=True):
            normalized = [
                None if fk_key_value(value) is NULL_FK_KEY else fk_key_value(value)
                for value in batch.column(child_col).to_pylist()
            ]
            remapped.append(
                mask_column(pa.array(normalized, from_pandas=True), seed, self._job_seed)
            )
        return tuple(remapped)

    def _replace_fk_columns(
        self, batch: pa.RecordBatch, fk_arrays: list[pa.Array]
    ) -> pa.RecordBatch:
        fields = list(batch.schema)
        arrays = list(batch.columns)
        for child_col, fk_array, dtype in zip(
            self._edge.child_columns, fk_arrays, self._output_types, strict=True
        ):
            idx = batch.schema.get_field_index(child_col)
            arrays[idx] = fk_array
            # Same field semantics as Table.set_column with a bare name (the
            # whole-child path): the field follows the new array's type.
            fields[idx] = pa.field(child_col, dtype)
        return pa.record_batch(arrays, schema=pa.schema(fields, metadata=batch.schema.metadata))


def _resolve_output_types(
    *,
    edge: RelationshipEdge,
    masked_types: tuple[pa.DataType, ...],
    child_key_types: tuple[pa.DataType, ...],
    remap_seeds: tuple[ColumnSeed, ...] | None,
) -> tuple[pa.DataType, ...]:
    """Fix one output type per FK component from schema-level candidates.

    Candidates per component: the relation's masked-key type always (matched
    rows), plus the fk_key_value-normalized child key type under PRESERVE/WARN
    (orphans keep their source key) or the parent strategy's output type under
    REMAP (orphans are re-masked). FAIL contributes nothing beyond the masked
    type: orphans raise before output and null keys are null under any type.
    """
    policy = edge.orphan_policy
    resolved: list[pa.DataType] = []
    for idx in range(len(edge.child_columns)):
        candidates = {_python_roundtrip_type(masked_types[idx])}
        if policy in (OrphanPolicy.PRESERVE, OrphanPolicy.WARN):
            # The orphan's OWN value goes through fk_key_value (`_append_
            # output_batch`'s PRESERVE/WARN branch), unlike a matched row's
            # value (the parent's masked value, verbatim, never fk_key_value'd)
            # -- so the round-trip type here must be the type fk_key_value
            # ACTUALLY produces, not the source column's own type.
            candidates.add(_fk_key_value_roundtrip_type(child_key_types[idx]))
        elif policy is OrphanPolicy.REMAP:
            if remap_seeds is None:
                raise AssertionError("remap seeds checked at construction")
            # The parent strategy's analytic output type over the normalized
            # child keys; re-round-tripped because remapped orphan values are
            # rebuilt from Python scalars like every other FK output value.
            # `_batch_remap_values` feeds fk_key_value-normalized values into
            # `mask_column`, so the "source type" a strategy like passthrough
            # sees is also the fk_key_value round-trip type, not the raw one.
            candidates.add(
                _python_roundtrip_type(
                    masked_output_type(
                        remap_seeds[idx], _fk_key_value_roundtrip_type(child_key_types[idx])
                    )
                )
            )
        resolved.append(_fixed_component_type(candidates))
    return tuple(resolved)


def _fixed_component_type(candidates: set[pa.DataType]) -> pa.DataType:
    non_null = {dtype for dtype in candidates if not pa.types.is_null(dtype)}
    if len(non_null) <= 1:
        merged = _unified_chunk_type(candidates)
        return merged if merged is not None else pa.null()
    if non_null == {pa.bool_(), pa.int64()}:
        # Codex round-5 Finding B: a matched row's masked value is a real
        # bool, verbatim; an orphan/REMAP-minted value went through
        # fk_key_value's unconditional bool -> int normalization, so a bool
        # parent covering only one of True/False makes the fixed type for
        # that edge genuinely int64, not bool. This is a LOCAL rule, not
        # delegated to `_unified_chunk_type` (which deliberately still
        # refuses bool/int64, matching a genuinely mixed whole column's own
        # pa.array() crash -- see that function's docstring): the schema
        # here must commit to one type before any data is seen, so it cannot
        # defer to "would the whole column actually crash." bool -> int64 is
        # always a lossless cast (True/False are exactly 1/0), so int64 is
        # the one type that represents every sub-case's values correctly
        # (all-matched, all-orphan, or a per-batch split of both).
        return pa.int64()
    if non_null <= {pa.int64(), pa.float64()}:
        # The whole-child path's own multi-type mix: an int64/float64 split
        # permissively merges to float64 whether or not the data mixes.
        merged = _unified_chunk_type(candidates)
        if merged is not None:
            return merged
    raise ExecutionError(
        code="out_of_core_fk_key_dtype_unsupported",
        message=(
            "out-of-core FK output cannot fix one Arrow type for "
            f"({', '.join(sorted(str(dtype) for dtype in non_null))}); a fixed "
            "schema must pick one type before any data is seen, and a "
            "permissive promotion reproduces whole-column inference only when "
            "the data actually mixes the candidates. Only int64 with float64 "
            "(the pinned whole-float narrowing) or bool with int64 (the pinned "
            "bool-orphan normalization) is allowed; any other mix, e.g. string "
            "with binary, would silently drift scalar values whenever a run "
            "exercises only the narrower candidate, so it is rejected before "
            "any output exists."
        ),
    )


def _fk_key_value_roundtrip_type(dtype: pa.DataType) -> pa.DataType:
    """The round-trip type of a value AFTER it passes through `fk_key_value`.

    Used only for the two call sites whose value genuinely goes through
    `fk_key_value` before becoming FK output: a PRESERVE/WARN orphan
    (`_append_output_batch`) and a REMAP mint's input (`_batch_remap_values`).
    A MATCHED row's value never does (it is the parent's masked value,
    verbatim), which is why `_resolve_output_types`'s masked_types candidate
    stays on the plain `_python_roundtrip_type` below.

    bool is the one type family `fk_key_value` unconditionally moves
    cross-family (`int(value)` in its bool branch, with no int-equality
    check gating it, unlike the whole-float narrowing) -- so a bool source
    column's fk_key_value round-trip type is int64, not bool (Codex round-5
    Finding B). Every other type is untouched by fk_key_value in a way that
    would change its round-trip image, so this defers to
    `_python_roundtrip_type` for everything else.
    """
    if pa.types.is_boolean(dtype):
        return pa.int64()
    return _python_roundtrip_type(dtype)


def _python_roundtrip_type(dtype: pa.DataType) -> pa.DataType:
    """The type `pa.array(values, from_pandas=True)` infers for values of dtype.

    The whole-child path rebuilds every FK output chunk from Python values, so
    parity requires candidate types in that same inference image. Types whose
    round-trip inference depends on the values (decimals digit-fit precision
    and scale; uint64 lands in int64 or uint64 by magnitude) have no fixed
    image and are rejected fail closed, as is anything unverified. This is the
    MATCHED-row image (a masked_types candidate, or REMAP's `masked_output_type`
    result); an orphan/REMAP-input value that passes through `fk_key_value`
    first needs `_fk_key_value_roundtrip_type` instead (its bool handling
    genuinely differs).
    """
    if pa.types.is_null(dtype):
        return dtype
    if pa.types.is_boolean(dtype):
        return pa.bool_()
    if pa.types.is_integer(dtype):
        if pa.types.is_uint64(dtype):
            raise _dtype_unsupported(dtype, "its Python round trip is value-dependent")
        return pa.int64()
    if pa.types.is_floating(dtype):
        return pa.float64()
    if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
        return pa.string()
    if pa.types.is_binary(dtype) or pa.types.is_large_binary(dtype):
        return pa.binary()
    if pa.types.is_timestamp(dtype):
        if dtype.tz is not None:
            raise _dtype_unsupported(dtype, "zone-aware round trips are not pinned by tests")
        return pa.timestamp("us")
    if pa.types.is_date32(dtype):
        return pa.date32()
    if pa.types.is_decimal(dtype):
        raise _dtype_unsupported(dtype, "inference digit-fits precision and scale from values")
    raise _dtype_unsupported(dtype, "no fixed inference image is established for it")


def _dtype_unsupported(dtype: pa.DataType, reason: str) -> ExecutionError:
    return ExecutionError(
        code="out_of_core_fk_key_dtype_unsupported",
        message=(
            f"out-of-core FK key type {dtype} cannot be typed up front ({reason}); "
            "rejected rather than allowed to drift from whole-column inference."
        ),
    )


def _cast_chunks(chunks: list[pa.Array], target: pa.DataType) -> pa.Array:
    if not chunks:
        return pa.array([], type=target)
    # cast_fk_chunk: an int64 chunk widened into a float64 fixed type (a whole
    # float orphan `fk_key_value` normalized to int, or a matched int key) uses
    # a representability-guarded unsafe cast rather than pyarrow's safe cast,
    # which would crash (ArrowInvalid) on any value beyond +/-2**53 even when it
    # is exactly representable -- a parity gap with the oracle's int64 output.
    return pa.concat_arrays([cast_fk_chunk(chunk, target) for chunk in chunks])


__all__ = ["ChildFkBatchJoiner"]

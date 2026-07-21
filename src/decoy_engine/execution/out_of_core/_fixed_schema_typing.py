"""Schema-fixed FK output typing for the batch-streaming sink path.

`StreamFkJoiner` (`_stream_join.py`) emits FK output batches to one Parquet
writer whose schema is fixed by the first batch, so every FK component needs
ONE Arrow type resolved up front, from schemas alone. `_resolve_output_types`
is that resolver and `_cast_chunks` casts each per-batch FK chunk onto it; both
were the typing core of the removed per-batch `ChildFkBatchJoiner` and are
lifted here unchanged (the only consumer is now `_stream_join.py`).

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

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE
from decoy_engine.execution.out_of_core._join import _unified_chunk_type, cast_fk_chunk
from decoy_engine.execution.out_of_core._mask import masked_output_type
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed
    from decoy_engine.relationships._graph import RelationshipEdge


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
        code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
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
        code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
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


__all__ = ["_cast_chunks", "_resolve_output_types"]

"""Conservative slim-sorter-row byte-width bound for out-of-core parent relations.

Lives apart from `_relation.py` (already within a handful of lines of the
600-LOC orchestration cap) and `_spill_estimate.py` (already at its
allowlisted 623-LOC ceiling with zero headroom) -- neither module can absorb
this without either crossing its cap or growing its allowlist entry for an
unrelated concern. Imports only pyarrow plus the sorter's own `_materialize`
(the same right-sizing gather the sorter measures rows through), so there is no
cycle risk in either direction.

`_route_policy.decide_route` reads the resulting
`ParentKeyRelation.max_sort_payload_row_bytes` (populated by
`SlimRowWidthTracker` at relation-build time) to decide whether a reordered
row would overflow the external sorter's per-merge-head cap and should fall
back to `_batch_join` instead of routing into a guaranteed
`_external_sort.py::out_of_core_sort_row_too_wide` raise.

After the slim-sort fix the sorter no longer carries the raw child columns
(`__decoy_fk_join_key`, `__decoy_src_*`), which are re-fetched out-of-line in
phase 3. The only sorter row it materializes is the SLIM row: `__decoy_row_nr`
(fixed 8B int64), the compact nullable-boolean match token, and the masked
PARENT components. So the tracked quantity is a conservative upper bound on the
entire materialized slim row's `nbytes` -- exactly what
`_external_sort.py::write` rejects on (`_materialize(view).nbytes` per row).

The bound is measured, not formula-derived: a synthetic single row is built at
the widest observed masked value width (per component, summed WITHIN the edge,
since composite components share one sorter row) and pushed through the same
`_materialize` gather the sorter uses, then a small per-array validity/offset
margin is added so the bound stays a true upper bound over any real row
(whose nulls or an upstream large-offset variant could add a few bytes the
all-valid synthetic omits). An empty or all-null relation still contributes the
schema-derived fixed + offset/validity overhead (never 0). A masked component
whose type the slim sorter could not bound (nested / extension / an
unsupported dictionary value type) makes the whole bound UNBOUNDED so the route
falls back rather than mapping such a column to 0.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Final

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution.out_of_core._external_sort_bounding import _materialize

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# A masked component the slim sorter cannot bound (see `_boundable_value_type`)
# forces the route to fall back; `sys.maxsize` compares `>= per_head_cap` for
# any realistic cap, so it reads as "never admit this edge to the sorter".
_UNBOUNDED_ROW_BYTES: Final = sys.maxsize

# Per-array headroom over the all-valid synthetic row: a real row's present
# validity bitmap (the synthetic row omits it) and an upstream large-offset
# variant add a few bytes the synthetic layout does not. 8 bytes/array is
# comfortably above the observed single-row delta and negligible against a
# per-head cap, so the bound stays a true upper bound without spuriously
# rejecting ordinary KB-scale keys.
_VALIDITY_MARGIN_PER_ARRAY: Final = 8


def _dictionary_value_type(dtype: pa.DataType) -> pa.DataType:
    """Resolve a dictionary type to its value type; pass anything else through.

    The masked value the sorter actually materializes is the decoded value, so
    a dictionary-encoded masked key is classified and sized by its value type,
    never by the (tiny) index.
    """
    return dtype.value_type if pa.types.is_dictionary(dtype) else dtype


def _boundable_value_type(dtype: pa.DataType) -> bool:
    """True for a resolved value type whose single-row width the slim bound can
    upper-bound. Nested/extension/unknown types cannot be sized here and force
    the whole bound UNBOUNDED (a fall-back), never a silent 0."""
    return (
        pa.types.is_integer(dtype)
        or pa.types.is_floating(dtype)
        or pa.types.is_temporal(dtype)
        or pa.types.is_decimal(dtype)
        or pa.types.is_fixed_size_binary(dtype)
        or pa.types.is_boolean(dtype)
        or pa.types.is_null(dtype)
        or pa.types.is_string(dtype)
        or pa.types.is_large_string(dtype)
        or pa.types.is_binary(dtype)
        or pa.types.is_large_binary(dtype)
    )


def _is_variable_width(dtype: pa.DataType) -> bool:
    return (
        pa.types.is_string(dtype)
        or pa.types.is_large_string(dtype)
        or pa.types.is_binary(dtype)
        or pa.types.is_large_binary(dtype)
    )


def _synthetic_value(dtype: pa.DataType, width: int) -> pa.Array:
    """A single-element array of `dtype`, variable-width types filled to `width`
    bytes so the synthetic slim row reflects the widest observed masked value."""
    if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
        return pa.array(["a" * width], type=dtype)
    if pa.types.is_binary(dtype) or pa.types.is_large_binary(dtype):
        return pa.array([b"a" * width], type=dtype)
    if pa.types.is_fixed_size_binary(dtype):
        return pa.array([b"\x00" * dtype.byte_width], type=dtype)
    if pa.types.is_null(dtype):
        return pa.nulls(1, type=dtype)
    if pa.types.is_boolean(dtype):
        return pa.array([True], type=dtype)
    # Every remaining boundable type is fixed-width and scalar; 0 is a valid,
    # representative value whose buffers are the type's fixed footprint.
    return pa.array([0], type=dtype).cast(dtype)


class SlimRowWidthTracker:
    """Streams a RecordBatch iterator through unchanged, recording the widest
    masked value per component so the slim sorter row's byte bound can be
    computed after the pass.

    The pre-dedup staging batches this wraps are a safe upper bound on the
    relation's deduped max: dedup only removes rows, so measuring before it can
    only over-, never under-, count.
    """

    def __init__(
        self, masked_columns: tuple[str, ...], masked_types: tuple[pa.DataType, ...]
    ) -> None:
        self._columns = masked_columns
        self._value_types = tuple(_dictionary_value_type(t) for t in masked_types)
        self._unbounded = any(not _boundable_value_type(t) for t in self._value_types)
        self._max_value_bytes = [0] * len(masked_columns)

    def wrap(self, batches: Iterable[pa.RecordBatch]) -> Iterator[pa.RecordBatch]:
        for batch in batches:
            if not self._unbounded:
                self._observe(batch)
            yield batch

    def _observe(self, batch: pa.RecordBatch) -> None:
        for idx, name in enumerate(self._columns):
            value_type = self._value_types[idx]
            if not _is_variable_width(value_type):
                continue
            column = batch.column(name)
            if pa.types.is_dictionary(column.type):
                column = column.cast(value_type)
            # pc.* funcs are dynamically generated; stubs miss them.
            widest = pc.max(pc.binary_length(column))  # type: ignore[attr-defined, unused-ignore]
            observed = widest.as_py()
            if observed is not None and observed > self._max_value_bytes[idx]:
                self._max_value_bytes[idx] = observed

    @property
    def max_sort_payload_row_bytes(self) -> int:
        """Conservative upper bound on the widest materialized slim sorter row.

        UNBOUNDED (a fall-back) when any masked component's type the sorter
        cannot size; otherwise the `_materialize`d `nbytes` of a synthetic row
        carrying `__decoy_row_nr`, the boolean match token, and every masked
        component at its widest observed width, plus a per-array margin."""
        if self._unbounded:
            return _UNBOUNDED_ROW_BYTES
        columns: list[pa.Array] = [
            pa.array([0], type=pa.int64()),
            pa.array([True], type=pa.bool_()),
        ]
        names = ["__decoy_row_nr", "__decoy_parent_match"]
        for idx, name in enumerate(self._columns):
            columns.append(_synthetic_value(self._value_types[idx], self._max_value_bytes[idx]))
            names.append(name)
        row = pa.record_batch(columns, names=names)
        margin = _VALIDITY_MARGIN_PER_ARRAY * len(columns)
        return _materialize(row).nbytes + margin


__all__ = ["_UNBOUNDED_ROW_BYTES", "SlimRowWidthTracker"]

"""Batch-bounding helpers for `_external_sort.py` (pure-move decomposition).

Moved out of `_external_sort.py` (P4 HIGH-1 module-size decomposition) to keep
that module under the 600-LOC orchestration cap; no behavior change. These are
the leaf functions `BoundedExternalSorter` uses to keep every buffered or
spilled batch byte-bounded: `_min_row_bytes`/`_is_supported_key_type` gate the
write-time key and schema checks, and `_materialize`/`_iter_bounded_views`/
`_bounded_batches` are the write-buffer and run-file batching primitives. See
`_external_sort.py`'s module docstring for the full memory contract these
functions implement.
"""

from __future__ import annotations

from collections.abc import Iterator

import pyarrow as pa
import pyarrow.compute as pc


def _min_row_bytes(schema: pa.Schema) -> int:
    """A LOWER bound on the byte width of the narrowest possible row of `schema`.

    Every column contributes its unavoidable per-row cost: a fixed-width column
    its byte width, a variable-width column (binary/string/list) only its offset
    entry (4 bytes for the int32-offset variants, 8 for the large/int64 ones),
    since a cell's data can be empty. Booleans and anything not recognized floor
    to 0. Each term is a true lower bound, so the sum never exceeds an actual
    row's width -- `_min_row_bytes(schema) >= INDEX_BYTES` therefore proves EVERY
    row is at least `INDEX_BYTES` wide, which is what keeps the sort index
    bounded after the flush sort reorders rows and `_bounded_batches`
    repartitions them (a narrow row could otherwise cluster into a post-sort run
    batch whose index dwarfs its data -- see the module docstring)."""
    total = 0
    for field in schema:
        t = field.type
        if pa.types.is_boolean(t):
            continue  # bit-packed, < 1 byte/row; floor at 0
        if (
            pa.types.is_integer(t)
            or pa.types.is_floating(t)
            or pa.types.is_temporal(t)
            or pa.types.is_decimal(t)
        ):
            total += t.bit_width // 8
        elif pa.types.is_fixed_size_binary(t):
            total += t.byte_width
        elif (
            pa.types.is_large_binary(t) or pa.types.is_large_string(t) or pa.types.is_large_list(t)
        ):
            total += 8  # int64 offset entry, data may be empty
        elif pa.types.is_binary(t) or pa.types.is_string(t) or pa.types.is_list(t):
            total += 4  # int32 offset entry, data may be empty
        # else: nested / unknown -> floor at 0 (a safe under-estimate)
    return total


def _is_supported_key_type(key_type: pa.DataType) -> bool:
    """True for the allowlisted orderable, total-order key types.

    Float is excluded on purpose: NaN has no total order, so the cutoff math is
    ill-defined and no consumer needs a float key. Everything else here has a
    well-defined Arrow ordering that `pc.max`/`pc.less_equal` respect.
    """
    return (
        pa.types.is_integer(key_type)  # signed + unsigned
        or pa.types.is_string(key_type)
        or pa.types.is_large_string(key_type)
        or pa.types.is_binary(key_type)
        or pa.types.is_large_binary(key_type)
        or pa.types.is_date(key_type)  # date32 + date64
        or pa.types.is_timestamp(key_type)
    )


def _materialize(batch: pa.RecordBatch) -> pa.RecordBatch:
    """Force a real, right-sized copy of `batch`.

    A zero-copy `RecordBatch.slice(...)` -- and a `Table.combine_chunks()`
    wrapping a single-chunk column, which is a no-op -- both keep the WHOLE
    parent batch's buffers alive. `pyarrow.compute.take` with an identity
    index is a genuine gather: it always allocates fresh buffers sized to
    just the requested rows, the one operation confirmed (by measuring the
    default memory pool before/after) to actually release the parent once
    the caller drops its own reference to the slice.
    """
    if batch.num_rows == 0:
        return batch
    identity = pa.array(range(batch.num_rows), type=pa.int64())
    return pc.take(batch, identity)


def _iter_bounded_views(batch: pa.RecordBatch, max_bytes: int) -> Iterator[pa.RecordBatch]:
    """Yield zero-copy sub-slices of `batch`, in original row order, each with
    `nbytes <= max_bytes` -- except a single-row slice that itself exceeds
    `max_bytes`, which is yielded anyway so the caller can identify and
    reject that exact row.

    Binary bisection rather than a per-row Python loop: `nbytes` on a
    zero-copy slice already reflects that slice's own logical byte content
    (a slice touching only small cells reports a small `nbytes` even when its
    parent batch also holds a huge cell elsewhere), so bisecting on it finds
    byte-bounded row ranges cheaply without materializing every candidate or
    looping row-by-row over millions of rows.
    """
    if batch.num_rows == 0:
        return
    if batch.num_rows == 1 or batch.nbytes <= max_bytes:
        yield batch
        return
    mid = batch.num_rows // 2
    yield from _iter_bounded_views(batch.slice(0, mid), max_bytes)
    yield from _iter_bounded_views(batch.slice(mid, batch.num_rows - mid), max_bytes)


def _bounded_batches(table: pa.Table, max_bytes: int) -> Iterator[pa.RecordBatch]:
    """Split `table` (already sorted by the caller) into materialized batches
    whose `nbytes` is `<= max_bytes`, in order. Used to write run files whose
    stored batches are safe to re-read one at a time as a bounded merge head."""
    for combined_batch in table.combine_chunks().to_batches():
        for view in _iter_bounded_views(combined_batch, max_bytes):
            yield _materialize(view)

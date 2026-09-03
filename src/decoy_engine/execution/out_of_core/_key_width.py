"""Conservative masked-key byte-width tracking for out-of-core parent relations.

Lives apart from `_relation.py` (already within a handful of lines of the
600-LOC orchestration cap) and `_spill_estimate.py` (already at its
allowlisted 623-LOC ceiling with zero headroom) -- neither module can absorb
this without either crossing its cap or growing its allowlist entry for an
unrelated concern. Imports nothing from either module, so there is no cycle
risk in either direction.

`_route_policy.decide_route` reads the resulting `ParentKeyRelation.
max_key_bytes` (populated by `MaxKeyWidthTracker` at relation-build time) to
decide whether a wide joined row would overflow the external sorter's
per-merge-head cap and should fall back to `_batch_join` instead of routing
into a guaranteed `_external_sort.py::out_of_core_sort_row_too_wide` raise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.compute as pc

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


def _row_key_byte_ceiling(batch: pa.RecordBatch, columns: tuple[str, ...]) -> int:
    """Upper bound on any single row's total key-column byte width in `batch`.

    Sums, per named column, the widest single value observed: the max
    string/binary length for variable-width types, `bit_width // 8` for
    fixed-width scalar types, 0 for anything else (a type this route's
    admitted key strategies never produce). An empty or all-null column
    contributes 0, not an error -- there is no widest value to report.
    """
    total = 0
    for name in columns:
        col_type = batch.column(name).type
        if (
            pa.types.is_string(col_type)
            or pa.types.is_large_string(col_type)
            or pa.types.is_binary(col_type)
            or pa.types.is_large_binary(col_type)
        ):
            widest = pc.max(  # type: ignore[attr-defined, unused-ignore]
                pc.binary_length(batch.column(name))  # type: ignore[attr-defined, unused-ignore]
            )
            total += widest.as_py() or 0
        else:
            total += (getattr(col_type, "bit_width", 0) or 0) // 8
    return total


class MaxKeyWidthTracker:
    """Streams a RecordBatch iterator through unchanged, recording the
    running max `_row_key_byte_ceiling` as each batch passes.

    The pre-dedup staging batches this wraps are a safe upper bound on the
    relation's deduped max: dedup only removes rows, so measuring before it
    can only over-, never under-, count.
    """

    def __init__(self, columns: tuple[str, ...]) -> None:
        self._columns = columns
        self.max_bytes = 0

    def wrap(self, batches: Iterable[pa.RecordBatch]) -> Iterator[pa.RecordBatch]:
        for batch in batches:
            self.max_bytes = max(self.max_bytes, _row_key_byte_ceiling(batch, self._columns))
            yield batch


__all__ = ["MaxKeyWidthTracker"]

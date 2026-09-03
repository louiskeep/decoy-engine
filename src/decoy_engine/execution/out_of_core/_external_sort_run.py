"""`_RunHead`, the spilled-run read cursor for `_external_sort.py` (pure-move
decomposition).

Moved out of `_external_sort.py` (P4 HIGH-1 module-size decomposition) to keep
that module under the 600-LOC orchestration cap; no behavior change. See
`_external_sort.py`'s module docstring for the full memory contract this class
participates in (`finish()`'s bounded k-way merge holds at most one stored
batch resident per open run).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc


class _RunHead:
    """One open run's read cursor during a merge pass: at most one stored
    batch resident at a time, refilled from disk only once fully consumed.

    Opened with `pa.OSFile` (a buffered, pool-tracked read), NOT
    `pa.memory_map`: a subprocess RSS measurement showed `memory_map` leaves
    the touched pages of the WHOLE mapped file resident (counted in VmHWM)
    even though pyarrow's own allocator never "sees" them as an allocation --
    reading a handful of small batches from each of several memory-mapped run
    files inflated real RSS by roughly the total on-disk run size, hundreds
    of MB above `pyarrow.default_memory_pool().max_memory()`'s own reported
    peak for the same run. `OSFile` reads copy each batch through the
    tracked pool instead, so the resident cost is bounded by what
    `peak_merge_resident_bytes` actually accounts for.
    """

    def __init__(self, path: Path, sort_key_column: str) -> None:
        self._sort_key_column = sort_key_column
        self._source = pa.OSFile(str(path), "rb")
        self._reader = pa.ipc.open_file(self._source)
        self._next_batch_idx = 0
        self.pending: pa.Table | None = None

    @property
    def is_final_batch(self) -> bool:
        """True once the currently pending batch is the last stored batch
        for this run (no more to load after it)."""
        return self._next_batch_idx >= self._reader.num_record_batches

    def ensure_loaded(self) -> None:
        if self.pending is not None and self.pending.num_rows > 0:
            return
        if self._next_batch_idx >= self._reader.num_record_batches:
            self.pending = None
            return
        batch = self._reader.get_batch(self._next_batch_idx)
        self._next_batch_idx += 1
        self.pending = pa.Table.from_batches([batch])

    def pending_table(self) -> pa.Table:
        """`self.pending`, narrowed to non-`None` for callers that already
        filtered on `pending is not None` (mypy cannot see across that
        filter's boundary, e.g. a list comprehension, on its own)."""
        assert self.pending is not None  # noqa: S101 -- caller filters by pending first
        return self.pending

    def max_value(self) -> pa.Scalar:
        """The largest key in the currently loaded batch, as a pyarrow Scalar of
        the key's exact type -- NOT `.as_py()`.

        The cutoff must stay a pyarrow scalar. Round-tripping through Python
        (`.as_py()`) re-encodes a `timestamp[ns]` value at microsecond precision
        (it becomes a `pandas.Timestamp` that `pc.less_equal` then truncates back
        to us), so the cutoff falls below a non-final head's own max, that head's
        sub-microsecond rows are never emitted, the head never refills, and the
        merge spins forever. Keeping it a scalar compares exactly for every
        allowlisted key type (and removes the latent Python-`min`-vs-Arrow-order
        assumption for string/binary)."""
        pending = self.pending_table()
        assert pending.num_rows > 0  # noqa: S101 -- caller checks first
        return pc.max(pending[self._sort_key_column])  # type: ignore[attr-defined, unused-ignore]

    def set_remaining(self, remaining: pa.Table) -> None:
        self.pending = remaining if remaining.num_rows > 0 else None

    def close(self) -> None:
        self._source.close()

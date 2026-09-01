"""The bounded external sorter (OOC-B milestone 1, key-generalized in P4-A.2).

`BoundedExternalSorter` sorts an unordered stream of Arrow batches by a single
orderable `sort_key_column` while keeping RESIDENT memory byte-capped at
`run_bytes_cap`, regardless of how many rows stream through or how wide any one
row is. It never changes a value, only the memory envelope. It generalizes the
original OOC-B `ExternalRowNrSorter` (which hard-wired an integer `__decoy_row_nr`
key) to any non-null orderable key type; `_reorder_budget.py` is the budget model
that sizes `run_bytes_cap` and `merge_fan_in`. Established method: external merge
sort (Knuth TAOCP v3 §5.4) -- run generation + fan-in-bounded k-way merge,
resident memory O(M) independent of N.

KEY CONTRACT (validated fail-closed at `write`-time, before any buffering or
spill). The generic cutoff math (`pc.max` -> `pc.min` over per-head max scalars
-> `pc.less_equal`, all pyarrow-scalar to stay type-exact) is only correct for a
non-null total order: a null key would make `pc.max` /
`pc.less_equal` yield a `None` cutoff or a null filter mask and silently drop
rows. So every `write(batch)` validates the key column first: it must exist, be
of an allowlisted orderable type (integer / string / large_string / binary /
large_binary / date / timestamp -- NOT float, whose NaN has no total order), hold
no nulls, and keep the same type across batches. DETERMINISM is the caller's
contract: because a multi-pass merge reshuffles rows across runs, a deterministic
output requires a TOTAL (unique) key -- the caller bakes any tiebreak into the
key column. Duplicate keys still sort correctly by key; only the tie order among
equal keys is unspecified. PARTITION-INVARIANCE: for a non-null total ascending
key the emitted order is independent of `run_bytes_cap` / `merge_fan_in` (those
change only how many runs/passes occur); the fail-closed cutoff guarantees a
globally correct merge at any fan-in.

MEMORY CONTRACT (each paragraph closes a Codex round-2 blocker):

`write()` never buffers a zero-copy slice of the caller's batch. A zero-copy
slice can report a small `nbytes` (accurate for its own logical content) while
still sharing buffers with the WHOLE parent batch -- a 10-row slice of a
batch that also holds one 5 MB cell elsewhere retains that whole 5 MB once
buffered, even though the slice's own 10 rows are tiny. Measured directly
against `pyarrow.default_memory_pool().bytes_allocated()`: a
`pyarrow.Table.combine_chunks()` wrapping an already-single-chunk slice (the
naive-looking fix) is a no-op and does NOT reclaim the parent's buffer;
`pyarrow.compute.take()` with an identity index is the operation that
actually performs a fresh, exactly-sized gather. Every buffered slice is
therefore materialized through `_materialize` (identity `take`), and the
buffer is flushed to a sorted, on-disk run BEFORE a slice would push it over
`run_bytes_cap`, never after -- the buffer itself never exceeds the cap. A
single row wider than the per-merge-head cap (`run_bytes_cap // (2 *
merge_fan_in)`) raises `out_of_core_sort_row_too_wide`: such a row cannot be
split into a bounded run-file batch, so it would become an over-cap stored batch
that a merge round co-loads `merge_fan_in` of, breaking the `run_bytes_cap`
envelope.

`finish()`'s k-way merge never holds more than one stored batch's worth of
data per open run. Every run file (initial or merged) is written in batches
capped at `_per_head_cap_bytes` (see below), so re-reading one batch per run
during a merge pass keeps the concurrently-open-run total bounded. Merging
combines only the CURRENTLY LOADED heads, computes how far the merge can
safely emit (the smallest "last value" among heads whose run still has more
data waiting on disk), writes that safe prefix, and only reloads a run's next
batch once its current one is fully consumed. More runs than `merge_fan_in`
trigger multiple bounded passes (a merge tree), never one wide merge.

`_per_head_cap_bytes` is `run_bytes_cap // (2 * merge_fan_in)`, not the
`run_bytes_cap // merge_fan_in` a first read of the budget model might
suggest: measuring the merge round's transient copy (concatenating the
active heads, then a single `sort_by` call to interleave them, which
internally computes sort indices and takes a fresh gathered copy while the
un-sorted heads are still resident) showed a roughly 2x transient over the
heads' own total, the same shape of overhead `SORT_OVERHEAD_FACTOR` documents
for the write-buffer flush. Halving the per-head cap keeps BOTH the heads
themselves and that transient gather within the single `run_bytes_cap`
ceiling. This is a deliberate, measured deviation from the milestone plan's
literal formula, not a loosened envelope -- it makes the *documented*
guarantee ("total resident across a fan-in-way merge is <= run_bytes_cap")
actually hold, which is the guarantee that matters.

CLEANUP: every run file the sorter creates (initial runs AND every intermediate
merge output of every pass) is registered in `_all_run_files` the moment its path
is allocated, before the file is opened. `close()` unlinks the whole registry, so
a partial `finish()` that fails mid-merge -- even after an earlier merge group
succeeded -- leaks nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._errors import ExecutionError

# Transient overhead multiplier for the flush-time sort: `Table.sort_by`
# computes a sort-indices array and gathers a freshly copied result table
# while the pre-sort buffer is still resident, so the true peak during a
# flush is higher than the buffer alone. Documented rather than derived:
# `sort_by` is a black-box compute call with no public hook to instrument its
# internal peak, so this factor is validated empirically instead (the
# wide-variable-row unit test and the Task 3 subprocess RSS proof).
SORT_OVERHEAD_FACTOR = 2.2

_RUN_FILE_PREFIX = "run"
_MERGE_FILE_PREFIX = "mergep"
_MERGE_SOURCE_COLUMN = "__decoy_merge_source"


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


class BoundedExternalSorter:
    """Sorts an unordered stream of Arrow batches by a single orderable
    `sort_key_column`, spilling to `spill_dir`, with resident memory capped at
    `run_bytes_cap` (both buffering and merging). See the module docstring for
    the key contract (non-null total order, validated fail-closed at write-time)
    and the full memory + cleanup contract."""

    def __init__(
        self,
        spill_dir: Path,
        run_bytes_cap: int,
        merge_fan_in: int,
        sort_key_column: str = "__decoy_row_nr",
    ) -> None:
        if run_bytes_cap <= 0:
            raise ExecutionError(
                code="out_of_core_reorder_budget_too_small",
                message=f"run_bytes_cap must be positive, got {run_bytes_cap}.",
            )
        if merge_fan_in < 2:
            raise ExecutionError(
                code="out_of_core_reorder_budget_too_small",
                message=f"merge_fan_in must be >= 2, got {merge_fan_in}.",
            )
        self._spill_dir = Path(spill_dir)
        self._run_bytes_cap = run_bytes_cap
        self._merge_fan_in = merge_fan_in
        self._sort_key_column = sort_key_column
        # See the module docstring's memory-contract closing paragraph: halved
        # so the merge round's transient concat+sort copy (roughly another
        # heads'-worth of bytes) still fits within run_bytes_cap alongside the
        # heads.
        self._per_head_cap_bytes = max(1, run_bytes_cap // (2 * merge_fan_in))

        self._buffered: list[pa.RecordBatch] = []
        self._buffered_bytes = 0
        self._peak_pre_sort_buffer_bytes = 0
        self._peak_buffered_bytes = 0
        self._peak_merge_resident_bytes = 0
        self._run_paths: list[Path] = []
        # Cleanup registry: EVERY run/merge file this sorter ever allocates,
        # registered in `_next_run_path` before the file is opened, so `close()`
        # cannot leak an intermediate that a partial `finish()` never consumed.
        self._all_run_files: set[Path] = set()
        self._run_counter = 0
        self._final_run_path: Path | None = None
        self._finished = False
        self._schema: pa.Schema | None = None
        self._key_type: pa.DataType | None = None

        self._spill_dir.mkdir(parents=True, exist_ok=True)

    @property
    def peak_buffered_bytes(self) -> int:
        """Max byte total ever held resident by this sorter: the write
        buffer (including the flush-time sort's transient overhead) and the
        merge phase's per-round head total, whichever is larger."""
        return max(self._peak_buffered_bytes, self._peak_merge_resident_bytes)

    @property
    def peak_pre_sort_buffer_bytes(self) -> int:
        """Max byte total the write buffer held BEFORE any flush-time sort
        overhead -- proves the buffer itself never exceeds `run_bytes_cap`,
        independent of the documented `SORT_OVERHEAD_FACTOR` transient."""
        return self._peak_pre_sort_buffer_bytes

    @property
    def peak_merge_resident_bytes(self) -> int:
        """Max combined byte total of the currently-open merge heads across
        any single merge round (excludes the write-buffer phase)."""
        return self._peak_merge_resident_bytes

    def _validate_key(self, batch: pa.RecordBatch) -> None:
        """Fail-closed key check, run before a batch is buffered or spilled.

        A missing / null / float / unsupported / drifting key would make the
        generic cutoff math silently drop rows (see the module KEY CONTRACT).
        """
        if self._sort_key_column not in batch.schema.names:
            raise ExecutionError(
                code="out_of_core_sort_key_missing",
                message=(
                    f"sort key column {self._sort_key_column!r} is not in the "
                    f"batch schema {batch.schema.names}."
                ),
            )
        key = batch.column(self._sort_key_column)
        if self._key_type is None:
            if not _is_supported_key_type(key.type):
                raise ExecutionError(
                    code="out_of_core_sort_key_type_unsupported",
                    message=(
                        f"sort key column {self._sort_key_column!r} has type "
                        f"{key.type}, which the bounded sorter does not support "
                        "(supported: integer, string, large_string, binary, "
                        "large_binary, date, timestamp; float is excluded because "
                        "NaN has no total order)."
                    ),
                )
            self._key_type = key.type
        elif key.type != self._key_type:
            raise ExecutionError(
                code="out_of_core_sort_key_type_drift",
                message=(
                    f"sort key column {self._sort_key_column!r} changed type from "
                    f"{self._key_type} to {key.type} between batches; the key type "
                    "must be stable for a well-defined ordering."
                ),
            )
        if key.null_count > 0:
            raise ExecutionError(
                code="out_of_core_sort_key_null",
                message=(
                    f"sort key column {self._sort_key_column!r} has "
                    f"{key.null_count} null value(s); a null key has no place in "
                    "a total order and would silently drop rows in the merge."
                ),
            )

    def write(self, batch: pa.RecordBatch) -> None:
        if self._finished:
            raise ExecutionError(
                code="out_of_core_sort_invalid_state",
                message="write() called after finish(); the sorter is read-only past that point.",
            )
        if batch.num_rows == 0:
            return
        self._validate_key(batch)
        if self._schema is None:
            self._schema = batch.schema
        # Bound the buffered views to `_per_head_cap_bytes`, not `run_bytes_cap`:
        # a single row wider than the per-head cap cannot be split into a
        # run-file batch that fits it, so it would become an over-cap stored
        # batch that the merge then co-loads `merge_fan_in` of -- blowing the
        # `run_bytes_cap` envelope by up to 2x. Reject such a row here, before it
        # can enter a run.
        for view in _iter_bounded_views(batch, self._per_head_cap_bytes):
            materialized = _materialize(view)
            row_bytes = materialized.nbytes
            if row_bytes > self._per_head_cap_bytes:
                raise ExecutionError(
                    code="out_of_core_sort_row_too_wide",
                    message=(
                        f"a single row is {row_bytes} bytes, over the "
                        f"{self._per_head_cap_bytes}-byte per-merge-head cap "
                        "(run_bytes_cap // (2 * merge_fan_in)); a wider row would "
                        "make one merge round's co-loaded heads exceed "
                        "run_bytes_cap. Increase the process memory budget or "
                        "shrink this column's width."
                    ),
                )
            if self._buffered and self._buffered_bytes + row_bytes > self._run_bytes_cap:
                self._flush()
            self._buffered.append(materialized)
            self._buffered_bytes += row_bytes
            self._peak_pre_sort_buffer_bytes = max(
                self._peak_pre_sort_buffer_bytes, self._buffered_bytes
            )
            self._peak_buffered_bytes = max(self._peak_buffered_bytes, self._buffered_bytes)

    def _next_run_path(self, prefix: str) -> Path:
        path = self._spill_dir / f"{prefix}_{self._run_counter:08d}.arrow"
        self._run_counter += 1
        # Register before the file is opened so `close()` never misses it, even
        # if writing the file raises.
        self._all_run_files.add(path)
        return path

    def _flush(self) -> None:
        if not self._buffered:
            return
        table = pa.Table.from_batches(self._buffered)
        pre_sort_bytes = self._buffered_bytes
        sorted_table = table.sort_by(self._sort_key_column)
        # The pre-sort buffer is still resident while sort_by's own indices
        # array and taken copy materialize (see SORT_OVERHEAD_FACTOR docs).
        self._peak_buffered_bytes = max(
            self._peak_buffered_bytes, pre_sort_bytes + sorted_table.nbytes
        )
        del table
        run_path = self._next_run_path(_RUN_FILE_PREFIX)
        self._write_run(sorted_table, run_path)
        self._run_paths.append(run_path)
        self._buffered = []
        self._buffered_bytes = 0

    def _write_run(self, table: pa.Table, path: Path) -> None:
        assert self._schema is not None  # noqa: S101 -- write() always sets this before any flush
        with pa.OSFile(str(path), "wb") as sink:
            writer = pa.ipc.new_file(sink, table.schema)
            try:
                for out_batch in _bounded_batches(table, self._per_head_cap_bytes):
                    if out_batch.num_rows:
                        writer.write_batch(out_batch)
            finally:
                writer.close()

    def finish(self) -> None:
        """Flush any remaining buffered rows, then merge every run into ONE
        ordered run via fan-in-bounded passes (a merge tree when there are
        more runs than `merge_fan_in`)."""
        if self._finished:
            return
        self._flush()
        current_runs = list(self._run_paths)
        pass_no = 0
        while len(current_runs) > 1:
            next_runs: list[Path] = []
            for start in range(0, len(current_runs), self._merge_fan_in):
                group = current_runs[start : start + self._merge_fan_in]
                if len(group) == 1:
                    next_runs.append(group[0])
                    continue
                merged_path = self._next_run_path(f"{_MERGE_FILE_PREFIX}{pass_no}")
                self._merge_group(group, merged_path)
                for old_path in group:
                    old_path.unlink(missing_ok=True)
                    self._all_run_files.discard(old_path)
                next_runs.append(merged_path)
            current_runs = next_runs
            pass_no += 1
        self._final_run_path = current_runs[0] if current_runs else None
        self._finished = True

    def _merge_group(self, run_paths: list[Path], output_path: Path) -> None:
        assert self._schema is not None  # noqa: S101 -- at least one run implies write() ran
        heads = [_RunHead(path, self._sort_key_column) for path in run_paths]
        try:
            with pa.OSFile(str(output_path), "wb") as sink:
                writer = pa.ipc.new_file(sink, self._schema)
                try:
                    self._merge_heads_into(heads, writer)
                finally:
                    writer.close()
        finally:
            for head in heads:
                head.close()

    def _merge_heads_into(
        self, heads: list[_RunHead], writer: pa.ipc.RecordBatchFileWriter
    ) -> None:
        while True:
            for head in heads:
                head.ensure_loaded()
            active = [head for head in heads if head.pending is not None]
            if not active:
                return
            resident = sum(head.pending_table().nbytes for head in active)
            self._peak_merge_resident_bytes = max(self._peak_merge_resident_bytes, resident)

            # Only a run with MORE data waiting on disk can still produce a
            # value smaller than what is already loaded; a run on its last
            # stored batch has nothing hidden left to compare against.
            constrained = [head.max_value() for head in active if not head.is_final_batch]
            # `pc.min` over the per-head max SCALARS, kept as a pyarrow scalar of
            # the key's exact type (see `_RunHead.max_value` -- a Python round-trip
            # truncates timestamp[ns] and hangs the merge).
            cutoff = pc.min(pa.array(constrained)) if constrained else None

            tagged = [
                head.pending_table().append_column(
                    _MERGE_SOURCE_COLUMN,
                    pa.array([i] * head.pending_table().num_rows, type=pa.int32()),
                )
                for i, head in enumerate(active)
            ]
            combined = pa.concat_tables(tagged).sort_by(self._sort_key_column)
            if cutoff is None:
                emit, keep = combined, combined.slice(0, 0)
            else:
                emit_mask = pc.less_equal(combined[self._sort_key_column], cutoff)  # type: ignore[attr-defined, unused-ignore]
                emit = combined.filter(emit_mask)
                keep = combined.filter(pc.invert(emit_mask))  # type: ignore[attr-defined, unused-ignore]

            for out_batch in _bounded_batches(
                emit.drop_columns([_MERGE_SOURCE_COLUMN]), self._per_head_cap_bytes
            ):
                if out_batch.num_rows:
                    writer.write_batch(out_batch)

            for i, head in enumerate(active):
                head_keep = keep.filter(pc.equal(keep[_MERGE_SOURCE_COLUMN], i))  # type: ignore[attr-defined, unused-ignore]
                head.set_remaining(head_keep.drop_columns([_MERGE_SOURCE_COLUMN]))

    def iter_ordered(self) -> Iterator[pa.RecordBatch]:
        """Yield the fully ordered result, one stored batch at a time.
        Requires `finish()` to have run first."""
        if not self._finished:
            raise ExecutionError(
                code="out_of_core_sort_invalid_state",
                message="iter_ordered() called before finish(); call finish() first.",
            )
        if self._final_run_path is None:
            return
        # pa.OSFile, not pa.memory_map: see _RunHead's docstring for the
        # measured reason (memory_map leaves the whole file's touched pages
        # resident, well beyond what pyarrow's own pool accounts for).
        with pa.OSFile(str(self._final_run_path), "rb") as source:
            reader = pa.ipc.open_file(source)
            for i in range(reader.num_record_batches):
                yield reader.get_batch(i)

    def close(self) -> None:
        """Remove every run file this sorter created -- initial runs and every
        intermediate merge output, tracked in `_all_run_files` -- so a partial
        `finish()` that failed mid-merge leaks nothing. Idempotent."""
        for path in self._all_run_files:
            path.unlink(missing_ok=True)
        self._all_run_files = set()
        self._run_paths = []
        self._final_run_path = None


__all__ = ["SORT_OVERHEAD_FACTOR", "BoundedExternalSorter"]

"""The bounded external sorter (OOC-B milestone 1, key-generalized in P4-A.2).

`BoundedExternalSorter` sorts an unordered stream of Arrow batches by a single
orderable `sort_key_column` while keeping RESIDENT memory byte-capped at
`run_bytes_cap`, regardless of how many rows stream through. A single row wider
than the per-merge-head cap (`run_bytes_cap // (2 * merge_fan_in)`) cannot be
bounded and is REJECTED at write-time (`out_of_core_sort_row_too_wide`, see the
memory contract below), so the cap does bound per-row width, not just the row
count. It never changes a value, only the memory envelope. It generalizes the
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
suggest: measuring the merge round's transient copy (concatenating each
head's rows that are safe to emit this round, then a single `sort_by` call to
interleave them, which internally computes sort indices and takes a fresh
gathered copy while the un-emitted head remainders are still resident) showed
a roughly 2x transient over the heads' own total, the same shape of overhead
`SORT_OVERHEAD_FACTOR` documents for the write-buffer flush. Halving the
per-head cap keeps BOTH the heads themselves and that transient gather within
the single `run_bytes_cap` ceiling. A `run_bytes_cap` so small that this
floor-divides to zero fails closed at construction
(`out_of_core_reorder_budget_too_small`) rather than silently clamping to a
1-byte cap that could never accept a real row. This is a deliberate, measured
deviation from the milestone plan's literal formula, not a loosened envelope.

`sort_by`'s index array is the reason the resident cap governs a per-row width,
not just a byte total. Arrow sorts by materializing a `uint64` (8-byte) index
per row, so a run of rows narrower than 8 bytes each (e.g. a bare int8 / int32 /
date32 key with no payload) makes that index dwarf the data the byte cap is
counting -- measured at ~8.9x the data for 1-byte rows, versus ~1.1x at 24-byte
rows -- and no byte-derived cap can bound it. It is NOT enough to check a
batch's AVERAGE row width: the flush sort REORDERS rows before `_bounded_batches`
cuts them into run batches, so a schema that mixes narrow (small-key) rows with
wide (large-key) rows can pass an average check yet cluster the narrow rows into
a post-sort run batch whose index dwarfs its data. `write()` therefore fails
closed (`out_of_core_sort_row_index_unbounded`) on the SCHEMA, up front:
`_min_row_bytes(schema)` (a lower bound over every possible row) must be
>= `INDEX_BYTES`. That proves EVERY row -- and hence every reordered run batch,
and every merge emit, whatever subset the cutoff selects -- carries at least an
8-byte index's worth of data, so the index stays <= the data the cap counts.
The bound is a real contract narrowing -- a caller wanting a narrow key column
pads it to an 8-byte-effective key or carries a fixed payload -- chosen (over
row-count-bounded spilling) because every actual consumer sorts >= 8-byte rows
and the degenerate case is not worth the extra spill machinery.

The merge holds NO per-row source-tag column. An earlier design tagged every
head's rows with an int32 run index, concatenated, sorted, then split the
un-emitted remainder back out by that tag; the 4-byte tag was itself unbounded
relative to a narrow key (it re-introduced exactly the index-array problem
above). Splitting each already-sorted head at the cutoff BEFORE the merge
(each head's remainder is trivially still attributed to its own run) needs no
tag, so the merge transient is just the emit gather.

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
from decoy_engine.execution.out_of_core._external_sort_bounding import (
    _bounded_batches,
    _is_supported_key_type,
    _iter_bounded_views,
    _materialize,
    _min_row_bytes,
)
from decoy_engine.execution.out_of_core._external_sort_run import _RunHead

# Transient overhead multiplier for the flush-time sort: `Table.sort_by`
# computes a sort-indices array and gathers a freshly copied result table
# while the pre-sort buffer is still resident, so the true peak during a
# flush is higher than the buffer alone. Documented rather than derived:
# `sort_by` is a black-box compute call with no public hook to instrument its
# internal peak, so this factor is validated empirically instead (the
# wide-variable-row unit test and the Task 3 subprocess RSS proof).
SORT_OVERHEAD_FACTOR = 2.2

# Bytes per row in Arrow's `sort_by` index array (a uint64 per row). A stored
# batch whose average row is narrower than this makes the sort index exceed the
# data the byte cap counts, so `write()` rejects it (see the module docstring's
# index-array paragraph).
INDEX_BYTES = 8

_RUN_FILE_PREFIX = "run"
_MERGE_FILE_PREFIX = "mergep"


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
        # See the module docstring's memory-contract paragraph: halved so the
        # merge round's transient concat+sort copy (roughly another
        # heads'-worth of bytes) still fits within run_bytes_cap alongside the
        # heads. A cap so small this floor-divides to zero cannot bound a single
        # row and is rejected here rather than clamped to a useless 1-byte cap.
        per_head_cap_bytes = run_bytes_cap // (2 * merge_fan_in)
        if per_head_cap_bytes < 1:
            raise ExecutionError(
                code="out_of_core_reorder_budget_too_small",
                message=(
                    f"run_bytes_cap={run_bytes_cap} is too small for "
                    f"merge_fan_in={merge_fan_in}: the per-merge-head cap "
                    "(run_bytes_cap // (2 * merge_fan_in)) rounds to 0 bytes, so "
                    "no row could ever be buffered. Increase the memory budget."
                ),
            )
        self._per_head_cap_bytes = per_head_cap_bytes

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
        """Coarse DATA-resident witness for the merge phase: the co-loaded heads
        plus the emit gather held concurrently. This is a lower bound on the true
        peak, not the enforced envelope -- like the flush (see
        `SORT_OVERHEAD_FACTOR`), sort_by's own index array is un-instrumentable
        through `nbytes`. The true merge peak is bounded by `run_bytes_cap *
        SORT_OVERHEAD_FACTOR` (the per-head cap's `// 2` leaves that headroom)
        and proved end-to-end by the subprocess RSS test; the `>= INDEX_BYTES`
        schema guard is what keeps that factor bounded. Excludes the write-buffer
        phase."""
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
            # The flush and merge both sort_by, and sort_by allocates an 8-byte
            # index per row. A row narrower than that makes the index exceed the
            # data the byte cap counts -- and because the flush sort reorders
            # rows before they are written into run batches, a schema that admits
            # any sub-8-byte row can cluster those rows into a post-sort run batch
            # the byte cap cannot bound. Fail closed on the SCHEMA (a lower bound
            # over every possible row), not per input batch, so no reordering can
            # smuggle a narrow run batch past the guard.
            min_row = _min_row_bytes(batch.schema)
            if min_row < INDEX_BYTES:
                raise ExecutionError(
                    code="out_of_core_sort_row_index_unbounded",
                    message=(
                        f"schema {batch.schema.names} admits rows as narrow as "
                        f"{min_row} bytes, under the {INDEX_BYTES}-byte sort index "
                        "per row; after the flush sort reorders rows, such rows "
                        "can cluster into a run batch whose index exceeds the "
                        "byte cap. The bounded sorter requires an effective row "
                        f"width of >= {INDEX_BYTES} bytes: widen the key (or carry "
                        "a fixed payload) so every row clears the sort index."
                    ),
                )
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
            cutoff = (
                pc.min(pa.array(constrained))  # type: ignore[attr-defined, unused-ignore]
                if constrained
                else None
            )

            # Split each head's already-ascending-sorted pending table at the
            # cutoff: rows <= cutoff are safe to emit this round, the rest stay
            # as that head's remainder. Because the head is sorted, the split is a
            # contiguous prefix, taken with ZERO-COPY slices -- emit and remainder
            # share the head's already resident buffer, adding no copy (a `filter`
            # would allocate a fresh emit AND a fresh remainder while the original
            # is still alive). Splitting per head also keeps every remainder
            # attributed to its own run WITHOUT a source-tag column (whose per-row
            # width would itself be unbounded relative to a narrow key -- see the
            # module docstring), so the only new merge allocation is the emit
            # gather below.
            emit_parts: list[pa.Table] = []
            for head in active:
                pending = head.pending_table()
                if cutoff is None:
                    emit_parts.append(pending)
                    head.set_remaining(pending.slice(0, 0))
                    continue
                # Count of keys <= cutoff = the prefix length (mask is bit-packed,
                # O(rows/8) bytes -- negligible against the byte cap).
                le = pc.sum(pc.less_equal(pending[self._sort_key_column], cutoff))  # type: ignore[attr-defined, unused-ignore]
                k = le.as_py() or 0
                emit_parts.append(pending.slice(0, k))
                head.set_remaining(pending.slice(k))

            combined = pa.concat_tables(emit_parts).sort_by(self._sort_key_column)
            # Coarse resident witness: the loaded heads plus the emit gather held
            # concurrently. Like the flush (see SORT_OVERHEAD_FACTOR), sort_by's
            # own index array and gather transient are un-instrumentable through
            # nbytes; the >= INDEX_BYTES schema guard keeps them bounded (emit
            # rows <= resident / INDEX_BYTES, so the index <= the resident heads),
            # and the true end-to-end peak is proved by the subprocess RSS test.
            # The per-head cap's `// 2` leaves the same headroom for this merge
            # transient that it leaves for the flush.
            self._peak_merge_resident_bytes = max(
                self._peak_merge_resident_bytes, resident + combined.nbytes
            )

            for out_batch in _bounded_batches(combined, self._per_head_cap_bytes):
                if out_batch.num_rows:
                    writer.write_batch(out_batch)

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


__all__ = ["INDEX_BYTES", "SORT_OVERHEAD_FACTOR", "BoundedExternalSorter"]

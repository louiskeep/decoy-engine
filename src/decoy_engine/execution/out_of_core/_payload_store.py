"""Masked-payload store for the single-source-read OOC-B driver (fix#2).

Phase 1 of `_stream_driver.py` masks each raw source batch exactly once, then
hands it here instead of discarding it; phase 3 resolves FK columns by reading
it back, so the source itself is never opened a second time. This is a
standard staging-table / spill pattern: buffer a stream once so a later pass
can revisit it without re-reading the original. `ResidentPayloadStore` is the
in-memory case (output already fits in memory, by definition, when there is no
sink); `SpillPayloadStore` writes an Arrow IPC record-batch stream file, the
established lossless columnar streaming format (`pa.ipc.new_stream` /
`open_stream`) -- unlike Parquet, it round-trips the Arrow schema and values
with no re-encoding, so the masked types captured in phase 1 come back
unchanged in phase 3. Neither implementation holds more than one masked batch
resident at read time.

Row numbering is never stored in-band: batches are appended in source-read
order and numbered contiguously from 0, so the k-th batch's row_nr offset is
just the running sum of the row counts of the batches before it. Both stores
compute that offset on read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import pyarrow as pa

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class PayloadStore(Protocol):
    """Forward-write / forward-read store of masked payload batches."""

    def append(self, batch: pa.RecordBatch) -> None:
        """Append one masked batch, in source-read order."""
        ...

    def iter_batches(self) -> Iterator[tuple[int, pa.RecordBatch]]:
        """Yield `(row_nr_start, batch)` in append order, row_nr_start running."""
        ...

    def close(self) -> None:
        """Release any resources (file handles); safe to call more than once."""
        ...


class ResidentPayloadStore:
    """In-memory payload store, used whenever the output has no sink.

    No disk, no type round-trip: batches are held exactly as masked, identical
    to what today's resident (no-sink) path already keeps in Python.
    """

    def __init__(self) -> None:
        self._batches: list[pa.RecordBatch] = []

    def append(self, batch: pa.RecordBatch) -> None:
        if batch.num_rows == 0:
            return
        self._batches.append(batch)

    def iter_batches(self) -> Iterator[tuple[int, pa.RecordBatch]]:
        offset = 0
        for batch in self._batches:
            yield offset, batch
            offset += batch.num_rows

    def close(self) -> None:
        self._batches = []


class SpillPayloadStore:
    """Arrow-IPC-backed payload store for the sink (bounded-residency) path.

    Writes one record-batch stream file under `path` in phase 1, reads it back
    sequentially in phase 3; at most one batch is resident on either side.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._writer: pa.ipc.RecordBatchStreamWriter | None = None
        self._schema: pa.Schema | None = None

    def append(self, batch: pa.RecordBatch) -> None:
        if batch.num_rows == 0:
            return
        if self._writer is None:
            # The schema is only known once the first non-empty batch arrives;
            # opening the writer any earlier would fix it from an empty batch's
            # (possibly wrong) type inference.
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._schema = batch.schema
            self._writer = pa.ipc.new_stream(str(self._path), self._schema)
        self._writer.write_batch(batch)

    def iter_batches(self) -> Iterator[tuple[int, pa.RecordBatch]]:
        # Finalize the stream before reading it back. An Arrow IPC stream is
        # only guaranteed complete once the writer is closed (its end-of-stream
        # marker written and its buffers flushed); reading a stream still open
        # for writing relies on the reader tolerating a missing EOS marker,
        # which is version and buffering dependent. The store is write-once in
        # phase 1 then read in phase 3, so closing here is a safe one-way
        # transition.
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._schema is None:
            # Nothing was ever appended (every batch was zero-row): no file was
            # created, so there is nothing to read back.
            return
        with pa.ipc.open_stream(str(self._path)) as reader:
            offset = 0
            for batch in reader:
                yield offset, batch
                offset += batch.num_rows

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None


class RawParentKeySpill:
    """Lossless, re-readable Arrow-IPC spill of the raw outgoing parent-key columns.

    Captured in the single phase-1 read so the outgoing-relation build derives
    its join keys from this spill, never a second read of the source (the
    same-count-permutation corruption that closes). Arrow IPC, NOT Parquet: an
    FK key may legitimately carry an Arrow type Parquet cannot encode
    (`month_day_nano_interval`, run-end-encoded, and the like) yet the route
    admits and the oracle masks, so a Parquet spill would crash inputs that
    otherwise succeed. IPC round-trips the full admitted Arrow surface.

    It is a re-readable `Iterable[pa.RecordBatch]` (`_relation.ParentSource`
    accepts that): each `__iter__` re-opens the finalized stream, so one
    relation build per outgoing edge can walk it independently, one batch
    resident at a time. Read only after `finalize()`; the stream must carry its
    end-of-stream marker before any reader opens it.
    """

    def __init__(self, path: Path, schema: pa.Schema) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Schema is known up front (the raw source key columns' fixed types),
        # so the writer opens eagerly; a zero-row table yields a valid empty
        # stream that reads back as no batches.
        self._writer: pa.ipc.RecordBatchStreamWriter | None = pa.ipc.new_stream(str(path), schema)

    def append(self, batch: pa.RecordBatch) -> None:
        if self._writer is None:
            raise AssertionError("append after finalize")
        if batch.num_rows:
            self._writer.write_batch(batch)

    def finalize(self) -> None:
        """Close the writer so the stream is complete; idempotent."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def __iter__(self) -> Iterator[pa.RecordBatch]:
        with pa.ipc.open_stream(str(self._path)) as reader:
            yield from reader


class SpillChildKeys:
    """Disk-backed, RE-OPENABLE Arrow-IPC spill of one edge's staged child keys.

    Replaces `StreamFkJoiner`'s `child_keys` DuckDB TEMP TABLE (OOC-B fix#1b):
    that table held one row per CHILD row -- an O(child) structure that, being
    a DuckDB temp table, could not fully evict its buffer-manager/control
    state, so it stayed resident for the whole child stream regardless of
    `memory_limit` (the regression `_stream_join.py`'s module docstring
    describes). This class makes the child side symmetric with the parent
    side: `parent_keys` is already a `read_parquet` VIEW over
    `ParentKeyRelation` (never materialized); the child now stages into this
    file instead, and each DuckDB scan gets a fresh streaming
    `RecordBatchReader` over it, so DuckDB's own external hash-join +
    merge-sort own the spilling instead of a resident Python/DuckDB structure.

    Arrow IPC, NOT Parquet, for the same reason `RawParentKeySpill` uses IPC
    for the parent-side raw keys: a child FK key column may legitimately carry
    an Arrow type Parquet cannot encode (`month_day_nano_interval`,
    run-end-encoded, and the like) that the route admits and the oracle masks.
    IPC round-trips the full admitted Arrow surface losslessly; a Parquet
    spill would crash inputs that otherwise succeed.

    `open_reader()` returns a FRESH single-pass `pa.RecordBatchReader` each
    call, reopening the finalized stream from byte 0 -- required because a
    DuckDB-registered `RecordBatchReader` is single-pass (rows on the first
    query against it, zero on a second), and `StreamFkJoiner` scans the child
    TWICE per edge (`total_orphans()`'s FAIL precount, then
    `iter_join_rows()`'s join), so each scan needs its own reader, never a
    shared one. Every call is independent of any earlier reader's position, so
    an early-aborted consumer never leaves the next `open_reader()` starting
    mid-stream. Read only after `finalize()`: the stream is only guaranteed
    complete (its end-of-stream marker written) once the writer is closed --
    reading one still open for writing is version/buffering dependent.
    """

    def __init__(self, path: Path, schema: pa.Schema) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Schema is known up front (`StreamFkJoiner._key_schema`, fixed before
        # any batch is staged), so the writer opens eagerly, mirroring
        # `RawParentKeySpill`; a zero-row table yields a valid empty stream
        # that reads back as no batches.
        self._writer: pa.ipc.RecordBatchStreamWriter | None = pa.ipc.new_stream(str(path), schema)

    def append(self, batch: pa.RecordBatch) -> None:
        if self._writer is None:
            raise AssertionError("append after finalize")
        if batch.num_rows:
            self._writer.write_batch(batch)

    def finalize(self) -> None:
        """Close the writer so the stream carries its EOS marker; idempotent."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def open_reader(self) -> pa.RecordBatchReader:
        """A fresh, single-pass reader over the finalized stream, from byte 0.

        Must be called after `finalize()`. The caller owns the returned
        reader (registering, then unregistering, it with its own DuckDB
        connection is `StreamFkJoiner`'s job, not this store's).
        """
        if self._writer is not None:
            raise AssertionError("open_reader before finalize")
        return pa.ipc.open_stream(str(self._path))


__all__ = [
    "PayloadStore",
    "RawParentKeySpill",
    "ResidentPayloadStore",
    "SpillChildKeys",
    "SpillPayloadStore",
]

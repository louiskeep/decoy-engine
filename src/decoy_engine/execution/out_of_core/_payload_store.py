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
        if self._writer is None:
            # Nothing was ever appended (or every batch was zero-row): no file
            # was created, so there is nothing to read back.
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


__all__ = ["PayloadStore", "ResidentPayloadStore", "SpillPayloadStore"]

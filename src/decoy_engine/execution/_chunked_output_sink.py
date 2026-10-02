"""Incremental output sink for the auto-chunk lane (Rust engine program slice B6a).

B2 and B7 run a routed table on B1's chunked dispatcher but still gather every masked
chunk and join them into one resident table. When the caller passes a
`TransactionalSink` (and nothing needs the whole output afterwards), this module
streams the chunks into it one Parquet row group at a time instead, and owns the
publish session that commits once at the end or aborts on any failure.

The design follows the established writers rather than inventing one:

- Arrow's `ParquetWriter` buffers one row group, not the file, and takes a fixed
  schema up front; `ParquetTransactionalSink.write_batches` is that pattern.
- DuckDB `COPY ... TO` and Polars `sink_parquet` flush a row group at a row (or byte)
  cap and bind the output schema before the first byte is written.
- Spark's `FileOutputCommitter` protocol stages output privately and makes it visible
  with one rename at job commit, deleting the staging tree on abort.

Decoy's oracle route cannot give every column's type up front (B1's schema rule pins
hash, truncate, string redact and passthrough columns only), so chunks that arrive
before every column has a non-null type are held back, and held-back chunks beyond one
row group spill to disk as Arrow IPC. Plan:
docs/plans/2026-10-02-b6a-incremental-output-sink.md.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._substrate import require_bool
from decoy_engine.execution._transactional_sink import TransactionalSink

if TYPE_CHECKING:
    from decoy_engine.execution._pipeline_multi_table import MultiTableSplit

__all__ = [
    "ROW_GROUP_BYTES",
    "ROW_GROUP_ROWS",
    "OutputFreeResults",
    "OutputPublish",
    "StreamStats",
    "decide_output_mode",
    "resident_block",
    "stream_table",
]

# pyarrow's documented `ParquetWriter` default row-group cap; a test fails if a pyarrow
# upgrade changes it, because byte identity with `pq.write_table` rests on it.
ROW_GROUP_ROWS = 1024 * 1024
# Protects wide rows, where 1,048,576 rows could be gigabytes. Frozen with the plan's bars.
ROW_GROUP_BYTES = 256 * 1024 * 1024

REASON_ELIGIBLE = "eligible"


def resident_block(reason: str) -> dict[str, Any]:
    return {"mode": "resident", "reason": reason}


def decide_output_mode(
    *,
    stream_chunked_output: bool,
    sink: Any,
    dispatcher_enabled: bool,
    config: dict[str, Any],
    fidelity_report: bool,
    post_validation: bool,
    split: MultiTableSplit | None = None,
    resident_names: Iterable[str] = (),
) -> tuple[str, str]:
    """`(mode, reason)`, decided once before the first chunk is masked.

    The first failing condition names the reason and the run stays resident. The
    consumers declined last (validators, quarantine, fidelity, post-validation) read the
    whole output after the mask step, so a run that has one cannot stream."""
    if not stream_chunked_output:
        return "resident", "streaming_disabled"
    if sink is None:
        return "resident", "no_sink"
    if not isinstance(sink, TransactionalSink):
        return "resident", "sink_not_streaming"
    if not dispatcher_enabled:
        return "resident", "legacy_lane"
    if split is not None:
        if split.full_frame:
            return "resident", "split_full_frame_present"
        if any(name not in split.dispatched for name in resident_names):
            return "resident", "split_extra_sources_present"
    if config.get("validators"):
        return "resident", "validators_present"
    if (config.get("quarantine") or {}).get("enabled"):
        return "resident", "quarantine_enabled"
    if fidelity_report:
        return "resident", "fidelity_report"
    if post_validation:
        return "resident", "post_validation"
    return "streamed", REASON_ELIGIBLE


class OutputPublish:
    """The publish session: inert until the run decides to stream, then commit-or-abort.

    Used as a context manager around everything from the mask step to the return. An
    exception leaving the block aborts an open, uncommitted session once (best effort,
    an abort error is suppressed) and propagates unchanged; `commit()` is the run's last
    action. The sink's `commit()` failing leaves the session uncommitted, so the same
    exit aborts it."""

    def __init__(self, sink: Any, stream: bool, post_validation: bool) -> None:
        require_bool("stream_chunked_output", stream)
        self.sink = sink
        self.stream = stream
        self.post_validation = post_validation
        self.is_open = False
        self._finished = False

    @property
    def active_sink(self) -> Any:
        """The sink when this run streams into it, else `None`."""
        return self.sink if self.is_open else None

    def open(self) -> None:
        self.is_open = True

    def commit(self) -> None:
        if not self.is_open or self._finished:
            return
        self.sink.commit()
        self._finished = True

    def __enter__(self) -> OutputPublish:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None and self.is_open and not self._finished:
            self._finished = True
            try:
                self.sink.abort()
            except Exception:
                pass


class OutputFreeResults(list):
    """B1's `chunk_result_sink`: keeps each chunk's evidence, never its output table."""

    def append(self, result: ExecutionResult) -> None:
        super().append(dataclasses.replace(result, outputs={}))


@dataclasses.dataclass
class StreamStats:
    row_groups: int = 0
    byte_cut_row_groups: int = 0
    held_back_chunks: int = 0
    spilled_chunks: int = 0

    def block(self) -> dict[str, Any]:
        return {"mode": "streamed", "reason": REASON_ELIGIBLE, **dataclasses.asdict(self)}


def _mismatch(table: str, detail: str) -> ExecutionError:
    return ExecutionError(
        code="chunked_schema_mismatch",
        message=f"cannot join the chunks of table {table!r}: {detail}.",
    )


class _Resolver:
    """B2's join rule applied incrementally: a column's target field is the field of the
    first chunk whose type is not `null`, else the first chunk's field. Columns the schema
    rule fixes count as resolved from the first chunk."""

    def __init__(self, table: str, fixed: frozenset[str]) -> None:
        self.table = table
        self.fixed = fixed
        self.names: list[str] | None = None
        self.first: list[pa.Field] = []
        self.fields: list[pa.Field | None] = []

    @property
    def resolved(self) -> bool:
        return self.names is not None and all(f is not None for f in self.fields)

    def observe(self, chunk: pa.Table) -> None:
        if self.names is None:
            self.names = chunk.column_names
            self.first = list(chunk.schema)
            self.fields = [None] * len(self.names)
        elif chunk.column_names != self.names:
            raise _mismatch(
                self.table,
                f"column names differ across chunks: {self.names} vs {chunk.column_names}",
            )
        for index, field in enumerate(chunk.schema):
            known = self.fields[index]
            if known is None:
                if not pa.types.is_null(field.type) or field.name in self.fixed:
                    self.fields[index] = field
            elif not pa.types.is_null(field.type) and field.type != known.type:
                types = sorted({str(known.type), str(field.type)})
                raise _mismatch(self.table, f"column {field.name!r} has disagreeing types {types}")

    def schema(self) -> pa.Schema:
        return pa.schema([f if f is not None else self.first[i] for i, f in enumerate(self.fields)])


@dataclasses.dataclass
class _Buffer:
    tables: list[pa.Table] = dataclasses.field(default_factory=list)
    rows: int = 0
    nbytes: int = 0


def _combine(parts: list[pa.Table]) -> pa.Table:
    return pa.concat_tables(parts).combine_chunks().replace_schema_metadata(None)


def _take_rows(buf: _Buffer, count: int) -> pa.Table:
    """Remove the first `count` rows from the buffer and return them as one table."""
    taken: list[pa.Table] = []
    need = count
    while need > 0:
        head = buf.tables[0]
        if head.num_rows <= need:
            taken.append(buf.tables.pop(0))
            need -= head.num_rows
        else:
            taken.append(head.slice(0, need))
            buf.tables[0] = head.slice(need)
            need = 0
    buf.rows -= count
    buf.nbytes = sum(t.nbytes for t in buf.tables)
    return _combine(taken)


def _feed(buf: _Buffer, chunk: pa.Table, stats: StreamStats) -> Iterator[pa.RecordBatch]:
    """Add one cast chunk; yield a record batch per completed row group."""
    buf.tables.append(chunk)
    buf.rows += chunk.num_rows
    buf.nbytes += chunk.nbytes
    while buf.rows >= ROW_GROUP_ROWS:
        stats.row_groups += 1
        yield from _take_rows(buf, ROW_GROUP_ROWS).to_batches()
    if buf.rows > 0 and buf.nbytes >= ROW_GROUP_BYTES:
        stats.row_groups += 1
        stats.byte_cut_row_groups += 1
        yield from _take_rows(buf, buf.rows).to_batches()


def _cast(chunk: pa.Table, schema: pa.Schema) -> pa.Table:
    return chunk if chunk.schema.equals(schema, check_metadata=True) else chunk.cast(schema)


class _Spill:
    """Held-back chunks beyond one row group, one Arrow IPC file each in a private
    directory. Read back with `OSFile`, not a memory map, so pages do not stay resident."""

    def __init__(self, stats: StreamStats) -> None:
        self.stats = stats
        self.directory: str | None = None
        self.paths: list[str] = []

    @property
    def active(self) -> bool:
        return self.directory is not None

    def add(self, chunk: pa.Table) -> None:
        if self.directory is None:
            self.directory = tempfile.mkdtemp(prefix="_decoy_hold_")
        path = os.path.join(self.directory, f"{len(self.paths):08d}.arrow")
        with pa.OSFile(path, "wb") as sink, pa.ipc.new_file(sink, chunk.schema) as writer:
            writer.write_table(chunk)
        self.paths.append(path)
        self.stats.spilled_chunks += 1

    def replay(self) -> Iterator[pa.Table]:
        for path in self.paths:
            with pa.OSFile(path, "rb") as source:
                table = pa.ipc.open_file(source).read_all()
            os.remove(path)
            yield table

    def close(self) -> None:
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None


def _hold_back(
    chunks: Iterator[pa.Table], resolver: _Resolver, spill: _Spill, stats: StreamStats
) -> list[pa.Table]:
    """Read chunks until every column has a type; return the in-memory ones (uncast).

    The resolving chunk is kept but not counted as held back. Once the in-memory chunks
    reach a row-group cap before resolution, they and every later unresolved chunk go to
    disk."""
    pending: list[pa.Table] = []
    rows = nbytes = 0
    for chunk in chunks:
        resolver.observe(chunk)
        if resolver.resolved:
            pending.append(chunk)
            break
        stats.held_back_chunks += 1
        if spill.active:
            spill.add(chunk)
            continue
        pending.append(chunk)
        rows += chunk.num_rows
        nbytes += chunk.nbytes
        if rows >= ROW_GROUP_ROWS or nbytes >= ROW_GROUP_BYTES:
            for held in pending:
                spill.add(held)
            pending.clear()
            rows = nbytes = 0
    return pending


def _row_group_batches(
    spill: _Spill,
    pending: list[pa.Table],
    chunks: Iterator[pa.Table],
    resolver: _Resolver,
    schema: pa.Schema,
    stats: StreamStats,
) -> Iterator[pa.RecordBatch]:
    """Replay spilled chunks, then the in-memory ones, then the live stream, as row groups."""
    buf = _Buffer()
    for chunk in spill.replay():
        yield from _feed(buf, _cast(chunk, schema), stats)
    while pending:
        yield from _feed(buf, _cast(pending.pop(0), schema), stats)
    for chunk in chunks:
        resolver.observe(chunk)
        yield from _feed(buf, _cast(chunk, schema), stats)
    if buf.rows > 0:
        stats.row_groups += 1
        yield from _take_rows(buf, buf.rows).to_batches()


def stream_table(
    sink: Any, table: str, chunks: Iterable[pa.Table], *, fixed_columns: frozenset[str]
) -> StreamStats:
    """Stream one routed table's masked chunks into `sink.write_batches`.

    Masking runs inside the sink's batch loop, so an error in chunk k propagates through
    `write_batches`. Raises `chunked_schema_mismatch` as `join_dispatcher_chunks` does,
    but at the offending chunk."""
    stats = StreamStats()
    resolver = _Resolver(table, fixed_columns)
    spill = _Spill(stats)
    live = iter(chunks)
    try:
        pending = _hold_back(live, resolver, spill, stats)
        if resolver.names is None:
            raise _mismatch(table, "no chunks to join")
        schema = resolver.schema()
        batches = _row_group_batches(spill, pending, live, resolver, schema, stats)
        sink.write_batches(table, batches, schema=schema)
    finally:
        spill.close()
    return stats

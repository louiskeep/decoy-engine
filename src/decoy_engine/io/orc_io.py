"""ORC sources and targets: bounded chunked reads, whole reads, and streamed writes.

Source patterns: Apache Arrow's own ORC support (`pyarrow.orc.ORCFile` for the file tail,
`pyarrow.dataset` ORC scanning for record batches below a stripe, which is Arrow C++
`ORCFileReader::GetRecordBatchReader(batch_size)`, and `ORCWriter` for incremental writes),
cut into owned chunks by `io.bounded_reader.cut_batch`.

Two checks pyarrow does not make are made here: the leading `ORC` magic, and timestamps.
pyarrow reads every ORC timestamp as `timestamp[ns]` and silently wraps a value outside
1677-09-21..2262-04-11, and exposes no column statistics that could prove a file in range, so a
file with a timestamp column is refused on read and an out-of-range value is refused on write.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from typing import Any

import pyarrow as pa

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccess, FileHandle, FileRequest, FileRole
from decoy_engine.fileaccess.adapters import arrow as arrow_adapter
from decoy_engine.io._target_types import orc_plan, prepare_chunk, resolve_chunks
from decoy_engine.io.bounded_reader import ChunkBudget, cut_batch
from decoy_engine.io.format_errors import OrcMalformed, OrcTypeUnsupported

MAGIC = b"ORC"


def _has_timestamp(t: pa.DataType) -> bool:
    if pa.types.is_timestamp(t):
        return True
    if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_map(t):
        return _has_timestamp(t.value_type) or (pa.types.is_map(t) and _has_timestamp(t.key_type))
    if pa.types.is_struct(t) or pa.types.is_union(t):
        return any(_has_timestamp(t.field(i).type) for i in range(t.num_fields))
    return False


def _library_failure(exc: BaseException) -> bool:
    """A failure that means "this is not a readable ORC file", as opposed to one a caller
    classifies by type (memory, disk, a policy decision), which must pass unchanged."""
    if fileaccess.keeps_class(exc) or isinstance(exc, MemoryError):
        return False
    return isinstance(exc, (OSError, pa.ArrowException, ValueError, fileaccess.FileAccessReadError))


def open_orc(handle: FileHandle, *, table: str) -> Any:
    """The file's tail, after the magic and the timestamp checks; no stripe is read."""
    with handle.open_binary() as fh:
        magic = fh.read(len(MAGIC))
    if magic != MAGIC:
        raise OrcMalformed(table=table, reason="magic")
    failed = False
    tail: Any = None
    try:
        tail = arrow_adapter.orc_file(handle)
    except Exception as exc:
        if not _library_failure(exc):
            raise
        failed = True
    if failed or tail is None:
        raise OrcMalformed(table=table, reason="tail")
    for field in tail.schema:
        if _has_timestamp(field.type):
            raise OrcTypeUnsupported(table=table, column=field.name, reason="timestamp")
    return tail


def _batches(handle: FileHandle, batch_size: int, *, table: str) -> Iterator[pa.RecordBatch]:
    iterator = arrow_adapter.orc_batches(handle, batch_size=batch_size)
    while True:
        failed = False
        batch: pa.RecordBatch | None = None
        try:
            batch = next(iterator)
        except StopIteration:
            return
        except Exception as exc:
            if not _library_failure(exc):
                raise
            failed = True
        if failed or batch is None:
            raise OrcMalformed(table=table, reason="stripe")
        yield batch


def _open(path: str, field_key: str) -> FileHandle:
    return fileaccess.current().open_read(FileRequest(FileRole.SOURCE, "read", path, field_key))


@fileaccess.entry_point
def iter_orc_chunks(
    path: str,
    budget: ChunkBudget,
    *,
    table: str,
    file_access: FileAccess | None = None,
    field_key: str = "source.path",
) -> Iterator[pa.Table]:
    """Owned chunks of an ORC file within `budget`, `budget.read_rows` rows decoded at a time."""
    with _open(path, field_key) as handle:
        open_orc(handle, table=table)
        row_base = 0
        for batch in _batches(handle, budget.read_rows, table=table):
            yield from cut_batch(batch, budget, table=table, row_base=row_base)
            row_base += batch.num_rows


def orc_head(handle: FileHandle, rows: int, *, table: str) -> pa.Table:
    """At least the first `rows` rows (all, if fewer), one record batch at a time."""
    tail = open_orc(handle, table=table)
    parts: list[pa.RecordBatch] = []
    gathered = 0
    for batch in _batches(handle, max(rows, 1), table=table):
        parts.append(batch)
        gathered += batch.num_rows
        if gathered >= rows:
            break
    out = pa.Table.from_batches(parts, schema=tail.schema) if parts else tail.schema.empty_table()
    return out.slice(0, rows)


def read_orc_handle(handle: FileHandle, *, table: str) -> pa.Table:
    """`read_orc` over a handle the caller's policy already authorized."""
    tail = open_orc(handle, table=table)
    batches = list(_batches(handle, 65_536, table=table))
    return pa.Table.from_batches(batches, schema=tail.schema)


@fileaccess.entry_point
def read_orc(
    path: str,
    *,
    table: str,
    file_access: FileAccess | None = None,
    field_key: str = "source.path",
) -> pa.Table:
    """The whole ORC file as one table."""
    with _open(path, field_key) as handle:
        return read_orc_handle(handle, table=table)


@fileaccess.entry_point
def write_orc(
    data: pa.Table | Iterable[pa.Table],
    path: str | os.PathLike[str],
    *,
    table: str,
    schema: pa.Schema | None = None,
    file_access: FileAccess | None = None,
) -> str:
    """Write `data` (one table, or chunks cast to `schema`) to an ORC file at `path`.

    Publishes atomically; on any refusal nothing is published and an existing file is left
    as it was. Returns `path` as `str`."""
    target = os.fspath(path)
    chunks, write_schema = resolve_chunks(data, schema)
    plan = orc_plan(write_schema, table=table)
    out = fileaccess.current().output_dir(
        FileRequest(FileRole.TARGET_OUTPUT, "write", target, "target.path")
    )
    with out.begin("file", "replace", label="table_sink") as txn:
        with arrow_adapter.orc_writer(txn, "orc") as writer:
            wrote = False
            for chunk in chunks:
                writer.write(prepare_chunk(chunk, plan, table=table))
                wrote = True
            if not wrote:
                writer.write(prepare_chunk(write_schema.empty_table(), plan, table=table))
        txn.publish()
    return target


__all__ = [
    "MAGIC",
    "iter_orc_chunks",
    "open_orc",
    "orc_head",
    "read_orc",
    "read_orc_handle",
    "write_orc",
]

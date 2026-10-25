"""PyArrow adapter: Parquet and Arrow IPC reads and writes over policy capabilities."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet as pq

from decoy_engine.fileaccess._errors import guarded, guarded_iter
from decoy_engine.fileaccess._types import FileHandle, OutputTxn


def _strict(handle: FileHandle) -> bool:
    return bool(getattr(handle, "strict", True))


def read_table(handle: FileHandle, **kwargs: Any) -> pa.Table:
    return guarded(handle.display, _strict(handle), pq.read_table, handle.library_path(), **kwargs)


def parquet_file(handle: FileHandle, **kwargs: Any) -> pq.ParquetFile:
    return guarded(handle.display, _strict(handle), pq.ParquetFile, handle.library_path(), **kwargs)


def iter_batches(
    handle: FileHandle, parquet_file: pq.ParquetFile, **kwargs: Any
) -> Iterator[pa.RecordBatch]:
    """`ParquetFile.iter_batches`, each step guarded under a strict handle."""
    source = guarded(handle.display, _strict(handle), parquet_file.iter_batches, **kwargs)
    return guarded_iter(handle.display, _strict(handle), source)


def read_metadata(handle: FileHandle) -> pq.FileMetaData:
    return guarded(handle.display, _strict(handle), pq.read_metadata, handle.library_path())


class _FileBacked:
    """A writer plus the file object it writes to; closing the writer closes both."""

    def __init__(self, writer: Any, fh: Any, display: str, strict: bool) -> None:
        self._writer = writer
        self._fh = fh
        self._display = display
        self._strict = strict

    def write_batch(self, batch: pa.RecordBatch, *args: Any, **kwargs: Any) -> None:
        guarded(self._display, self._strict, self._writer.write_batch, batch, *args, **kwargs)

    def write_table(self, table: pa.Table, *args: Any, **kwargs: Any) -> None:
        guarded(self._display, self._strict, self._writer.write_table, table, *args, **kwargs)

    def write(self, data: Any) -> None:
        guarded(self._display, self._strict, self._writer.write, data)

    def close(self) -> None:
        try:
            guarded(self._display, self._strict, self._writer.close)
        finally:
            self._fh.close()

    def __enter__(self) -> _FileBacked:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _writable(txn: OutputTxn, component: str) -> Any:
    return os.fdopen(txn.create_file(component), "wb")


def _readable(txn: OutputTxn, component: str) -> Any:
    return os.fdopen(txn.open_file(component), "rb")


def write_table(table: pa.Table, txn: OutputTxn, component: str, **kwargs: Any) -> None:
    """`pq.write_table` into a new file of the stage; bytes match a path write."""
    with _writable(txn, component) as fh:
        guarded(txn.display, txn.strict, pq.write_table, table, fh, **kwargs)


def parquet_writer(txn: OutputTxn, component: str, schema: pa.Schema, **kwargs: Any) -> _FileBacked:
    fh = _writable(txn, component)
    try:
        writer = guarded(txn.display, txn.strict, pq.ParquetWriter, fh, schema, **kwargs)
    except BaseException:
        fh.close()
        raise
    return _FileBacked(writer, fh, txn.display, txn.strict)


def ipc_new_file(txn: OutputTxn, component: str, schema: pa.Schema) -> _FileBacked:
    fh = _writable(txn, component)
    try:
        writer = guarded(txn.display, txn.strict, pa.ipc.new_file, fh, schema)
    except BaseException:
        fh.close()
        raise
    return _FileBacked(writer, fh, txn.display, txn.strict)


def ipc_new_stream(txn: OutputTxn, component: str, schema: pa.Schema) -> _FileBacked:
    fh = _writable(txn, component)
    try:
        writer = guarded(txn.display, txn.strict, pa.ipc.new_stream, fh, schema)
    except BaseException:
        fh.close()
        raise
    return _FileBacked(writer, fh, txn.display, txn.strict)


def ipc_read_file(txn: OutputTxn, component: str) -> pa.Table:
    """Read a whole IPC file written into this stage back as a table."""
    with _readable(txn, component) as fh:
        reader = guarded(txn.display, txn.strict, pa.ipc.open_file, fh)
        return guarded(txn.display, txn.strict, reader.read_all)


def ipc_read_stream_batches(txn: OutputTxn, component: str) -> list[pa.RecordBatch]:
    with _readable(txn, component) as fh:
        reader = guarded(txn.display, txn.strict, pa.ipc.open_stream, fh)
        return [b for b in reader]

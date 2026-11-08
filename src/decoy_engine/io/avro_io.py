"""Avro sources and targets: bounded chunked reads, whole reads, and streamed writes.

Source patterns: the Avro specification's object container format (framed by
`io.avro_container`, admitted by `io.avro_schema`), Hadoop's split-at-sync reading (a file is
cut into groups of whole blocks and each group is decoded on its own), and Arrow's bounded
batch cutting (`io.bounded_reader.cut_batch`). Records are decoded by Polars (`pl.read_avro`):
the engine never decodes a value itself. Polars has no block API, so each group is rebuilt as a
small container in memory (header plus the group's blocks, already decompressed by the engine,
codec `null`) and handed to Polars from an in-memory buffer.

Writing: each chunk is encoded by Polars into a private hold stage beside the target (through
the polars adapter, as a capability), read back as bytes, its blocks checked by the framer and
appended under the engine's own header (record `topLevelRecord`, UTC timestamps labelled
`timestamp-*`, Spark's `toAvroType` convention) and sync marker. The target publishes
atomically; the hold stage is always aborted.
"""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import polars as pl
import pyarrow as pa

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccess, FileHandle, FileRequest, FileRole, OutputTxn
from decoy_engine.fileaccess.adapters import polars as polars_adapter
from decoy_engine.io import avro_container as container
from decoy_engine.io import avro_schema
from decoy_engine.io._target_types import (
    TargetPlan,
    avro_plan,
    prepare_chunk,
    resolve_chunks,
)
from decoy_engine.io.bounded_reader import ChunkBudget, cut_batch
from decoy_engine.io.format_errors import (
    AvroBlockTooLarge,
    AvroMalformed,
    AvroSchemaUnsupported,
    TargetSchemaMismatch,
)

# A group is held about four times while it is decoded (the payload, Polars' frame with 16-byte
# string views, the Arrow export and its normalised copy), so groups are sized at a quarter of
# the budget by the per-column estimate. The RSS acceptance test is the real bound.
_DECODE_FACTOR = 4
_FULL_GROUP_BYTES = 64 * 1024 * 1024
_FULL_GROUP_ROWS = 1 << 30
_SYNC_LABEL = b"\xffdecoy.avro.sync/v1"
# Rows per written block target this many Arrow bytes, so a reader with a modest chunk budget
# can read the file back block by block (the Java writer's default sync interval is 64 KB).
_TARGET_BLOCK_BYTES = 64 * 1024
_REMEDY = "raise the chunk budget or read without chunking"


@dataclass(frozen=True)
class _Limits:
    """How one read groups blocks: the per-block cap, the group's estimated decoded bytes and
    rows, and whether the read is bounded (the chunked route: nested fields and a block whose
    estimate alone exceeds the group are refused; the whole-file and sample reads take both)."""

    block_cap: int
    group_bytes: int
    group_rows: int
    bounded: bool
    remedy: str | None = None


def _limits_for(budget: ChunkBudget) -> _Limits:
    cap = min(budget.chunk_budget_bytes, container.MAX_BLOCK_BYTES)
    return _Limits(cap, budget.chunk_budget_bytes, budget.max_chunk_rows, True, _REMEDY)


_FULL = _Limits(container.MAX_BLOCK_BYTES, _FULL_GROUP_BYTES, _FULL_GROUP_ROWS, False)


def _arrow_type(t: pa.DataType) -> pa.DataType:
    """The type a Parquet read of the same data gives: 32-bit offsets, dictionaries decoded."""
    if pa.types.is_dictionary(t):
        return _arrow_type(t.value_type)
    if pa.types.is_large_string(t) or pa.types.is_string_view(t):
        return pa.string()
    if pa.types.is_large_binary(t) or pa.types.is_binary_view(t):
        return pa.binary()
    if pa.types.is_large_list(t) or pa.types.is_list(t):
        return pa.list_(_arrow_field(t.value_field))
    if pa.types.is_struct(t):
        return pa.struct([_arrow_field(t.field(i)) for i in range(t.num_fields)])
    return t


def _arrow_field(f: pa.Field) -> pa.Field:
    return pa.field(f.name, _arrow_type(f.type), f.nullable)


def _normalize(table: pa.Table, *, name: str) -> pa.Table:
    target = pa.schema([_arrow_field(f) for f in table.schema])
    failed = False
    try:
        out = table.cast(target)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
        failed = True
    if failed:
        raise AvroMalformed(table=name, reason="decode_type")
    return out


def _decode_group(payload: bytes, *, name: str) -> pa.Table:
    """Decode one in-memory container (null codec) with Polars. Opens no file."""
    failed = False
    arrow: pa.Table | None = None
    try:
        frame = pl.read_avro(io.BytesIO(payload))
        arrow = frame.to_arrow(compat_level=pl.CompatLevel.oldest())
        del frame
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except BaseException:
        # Polars reports some bad input as a Rust panic (`PanicException`, a BaseException).
        failed = True
    if failed or arrow is None:
        raise AvroMalformed(table=name, reason="decode")
    return _normalize(arrow, name=name)


class _Source:
    """One opened Avro file: its header, admitted schema and the null-codec header bytes."""

    def __init__(self, handle: FileHandle, stream: BinaryIO, *, name: str, limits: _Limits):
        self.name = name
        self.length = handle.stat().st_size
        self.stream = stream
        self.header = container.read_header(stream, self.length, table=name)
        self.admission = avro_schema.admit(self.header.schema_json, table=name)
        if self.admission.nested and limits.bounded:
            raise AvroSchemaUnsupported(table=name, reason="nested_on_chunked_route")
        self.null_header = container.encode_header(self.header.schema_json, self.header.sync)

    def empty(self) -> pa.Table:
        return _decode_group(self.null_header, name=self.name)

    def blocks(self, *, skip_data: bool = False) -> Iterator[container.Block]:
        return container.iter_blocks(
            self.stream,
            self.header,
            self.length,
            table=self.name,
            min_row_bytes=self.admission.min_row_bytes,
            skip_data=skip_data,
        )

    def groups(self, limits: _Limits) -> Iterator[pa.Table]:
        """Decoded groups of whole blocks, each within `limits` by its estimate."""
        pending: list[bytes] = []
        estimate = 0
        rows = 0
        for block in self.blocks():
            if len(block.data) > limits.block_cap:
                raise AvroBlockTooLarge(**self._too_large(block, limits))
            raw = self._decompress(block, limits)
            cost = _DECODE_FACTOR * (len(raw) + block.count * self.admission.fixed_row_cost)
            if limits.bounded and cost > limits.group_bytes:
                # Alone in its group it would still decode past the budget.
                raise AvroBlockTooLarge(**self._too_large(block, limits))
            if pending and (
                estimate + cost > limits.group_bytes or rows + block.count > limits.group_rows
            ):
                yield self._decode(pending)
                estimate, rows = 0, 0
            pending.append(container.encode_block(block.count, raw, self.header.sync))
            estimate += cost
            rows += block.count
        if pending:
            yield self._decode(pending)

    def _too_large(self, block: container.Block, limits: _Limits) -> dict[str, object]:
        detail: dict[str, object] = {"table": self.name, "block": block.index, "limit": "budget"}
        if limits.remedy is not None:
            detail["remedy"] = limits.remedy
        return detail

    def _decompress(self, block: container.Block, limits: _Limits) -> bytes:
        over = False
        raw = b""
        try:
            raw = container.decompress(
                block,
                self.header.codec,
                limits.block_cap,
                table=self.name,
                min_row_bytes=self.admission.min_row_bytes,
            )
        except AvroBlockTooLarge:
            over = True
        if over:
            raise AvroBlockTooLarge(**self._too_large(block, limits))
        return raw

    def _decode(self, blocks: list[bytes]) -> pa.Table:
        # Empty the caller's list before decoding, so the blocks and the joined payload are
        # not both held while Polars decodes.
        payload = self.null_header + b"".join(blocks)
        blocks.clear()
        return _decode_group(payload, name=self.name)


def _open(path: str, field_key: str) -> FileHandle:
    return fileaccess.current().open_read(FileRequest(FileRole.SOURCE, "read", path, field_key))


@fileaccess.entry_point
def iter_avro_chunks(
    path: str,
    budget: ChunkBudget,
    *,
    table: str,
    file_access: FileAccess | None = None,
    field_key: str = "source.path",
) -> Iterator[pa.Table]:
    """Owned chunks of an Avro file within `budget`, one group of whole blocks at a time.

    Flat schemas only; a block larger than the budget is refused (`avro_block_too_large`)
    before it is decompressed."""
    with _open(path, field_key) as handle, handle.open_binary() as stream:
        limits = _limits_for(budget)
        source = _Source(handle, stream, name=table, limits=limits)
        row_base = 0
        for decoded in source.groups(limits):
            batches = decoded.to_batches()
            del decoded
            while batches:
                batch = batches.pop(0)
                yield from cut_batch(batch, budget, table=table, row_base=row_base)
                row_base += batch.num_rows
                del batch


@fileaccess.entry_point
def read_avro(
    path: str,
    *,
    table: str,
    file_access: FileAccess | None = None,
    field_key: str = "source.path",
) -> pa.Table:
    """The whole Avro file as one table (nested fields admitted; blocks up to 256 MiB)."""
    with _open(path, field_key) as handle, handle.open_binary() as stream:
        return read_avro_handle(handle, stream, table=table)


def read_avro_handle(handle: FileHandle, stream: BinaryIO, *, table: str) -> pa.Table:
    """`read_avro` over a handle the caller's policy already authorized."""
    source = _Source(handle, stream, name=table, limits=_FULL)
    parts = list(source.groups(_FULL))
    return pa.concat_tables(parts) if parts else source.empty()


def avro_schema_of(handle: FileHandle, stream: BinaryIO, *, table: str) -> pa.Schema:
    """The Arrow schema the reader produces, from the header alone (no block is read)."""
    return _Source(handle, stream, name=table, limits=_FULL).empty().schema


def avro_row_count(handle: FileHandle, stream: BinaryIO, *, table: str) -> int:
    """Rows claimed by the block headers, seeking over every block's data (no decode)."""
    source = _Source(handle, stream, name=table, limits=_FULL)
    return sum(block.count for block in source.blocks(skip_data=True))


def avro_head(handle: FileHandle, stream: BinaryIO, rows: int, *, table: str) -> pa.Table:
    """At least the first `rows` rows (all, if fewer), decoding whole groups of blocks under
    the whole-file admission, so a nested or single-block file still samples."""
    source = _Source(handle, stream, name=table, limits=_FULL)
    limits = _Limits(container.MAX_BLOCK_BYTES, _FULL_GROUP_BYTES, max(rows, 1), False)
    parts: list[pa.Table] = []
    gathered = 0
    for decoded in source.groups(limits):
        parts.append(decoded)
        gathered += decoded.num_rows
        if gathered >= rows:
            break
    out = pa.concat_tables(parts) if parts else source.empty()
    return out.slice(0, rows)


# --- writing ---------------------------------------------------------------------------------


def _sync_marker(schema_json: bytes, first_block: bytes) -> bytes:
    # Deterministic for the same schema and data; mixing in the first block means a value
    # cannot be crafted to equal the marker without predicting that block.
    return hashlib.sha256(_SYNC_LABEL + schema_json + first_block).digest()[: container.SYNC_SIZE]


def _encode(
    frame: pl.DataFrame, hold: OutputTxn, part: str, *, name: str
) -> tuple[bytes, list[container.Block]]:
    """Polars' encoding of one chunk, via the hold stage: its schema bytes and blocks."""
    polars_adapter.write_avro(frame, hold, part)
    with os.fdopen(hold.open_file(part), "rb") as fh:
        raw = fh.read()
    hold.remove(part)
    stream = io.BytesIO(raw)
    header = container.read_header(stream, len(raw), table=name)
    if frame.height == 0:
        return header.schema_json, []
    return header.schema_json, list(container.iter_blocks(stream, header, len(raw), table=name))


@fileaccess.entry_point
def write_avro(
    data: pa.Table | Iterable[pa.Table],
    path: str | os.PathLike[str],
    *,
    table: str,
    schema: pa.Schema | None = None,
    file_access: FileAccess | None = None,
) -> str:
    """Write `data` (one table, or chunks cast to `schema`) to an Avro file at `path`.

    Publishes atomically; on any refusal nothing is published and an existing file is left
    as it was. Returns `path` as `str`."""
    target = os.fspath(path)
    chunks, write_schema = resolve_chunks(data, schema)
    plan = avro_plan(write_schema, table=table)
    out = fileaccess.current().output_dir(
        FileRequest(FileRole.TARGET_OUTPUT, "write", target, "target.path")
    )
    hold_dir = fileaccess.current().output_dir(
        FileRequest(
            FileRole.TARGET_OUTPUT, "write", str(Path(target).parent / "_hold"), "targets.hold"
        )
    )
    with out.begin("file", "replace", label="table_sink") as txn:
        hold = hold_dir.begin("dir", "exclusive", label="hold")
        try:
            _write_all(chunks, plan, txn, hold, name=table)
        finally:
            hold.abort()
        txn.publish()
    return target


def _write_all(
    chunks: Iterable[pa.Table], plan: TargetPlan, txn: OutputTxn, hold: OutputTxn, *, name: str
) -> None:
    with os.fdopen(txn.create_file("avro"), "wb") as fh:
        sync = b""
        schema_json: bytes | None = None
        for index, chunk in enumerate(_frames(chunks, plan, name=name)):
            polars_schema, blocks = _encode(chunk, hold, f"{index:08d}.avro", name=name)
            if schema_json is None:
                schema_json = avro_schema.rewrite_for_write(polars_schema, plan.utc_columns)
                sync = _sync_marker(schema_json, blocks[0].data if blocks else b"")
                fh.write(container.encode_header(schema_json, sync))
            elif avro_schema.rewrite_for_write(polars_schema, plan.utc_columns) != schema_json:
                raise TargetSchemaMismatch(table=name, reason="encoded_schema_drift")
            for block in blocks:
                fh.write(container.encode_block(block.count, block.data, sync))
        fh.flush()
        os.fsync(fh.fileno())


def _frames(chunks: Iterable[pa.Table], plan: TargetPlan, *, name: str) -> Iterator[pl.DataFrame]:
    """Each chunk prepared for Polars, sliced to about `_TARGET_BLOCK_BYTES` per block; an empty
    write still yields one empty frame (for the header)."""
    emitted = False
    for chunk in chunks:
        emitted = True
        prepared = prepare_chunk(chunk, plan, table=name)
        if prepared.num_rows == 0:
            yield _to_polars(prepared)
            continue
        per_row = max(1, prepared.nbytes // prepared.num_rows)
        step = max(1, _TARGET_BLOCK_BYTES // per_row)
        for start in range(0, prepared.num_rows, step):
            yield _to_polars(prepared.slice(start, step))
    if not emitted:
        yield _to_polars(prepare_chunk(plan.schema.empty_table(), plan, table=name))


def _to_polars(table: pa.Table) -> pl.DataFrame:
    return pl.DataFrame(table)


__all__ = [
    "avro_head",
    "avro_row_count",
    "avro_schema_of",
    "iter_avro_chunks",
    "read_avro",
    "read_avro_handle",
    "write_avro",
]

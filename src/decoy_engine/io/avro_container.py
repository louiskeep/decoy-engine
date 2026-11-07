"""Avro object container framing: header, blocks and codecs, without decoding a record.

Source pattern: the Avro specification's "Object Container Files" section (header = magic
`Obj\\x01`, a metadata map holding `avro.schema` and `avro.codec`, a 16-byte sync marker; then
blocks of `long count, long size, bytes, sync`). Splitting a file at block boundaries is how
Hadoop's `AvroInputFormat` and Java's `DataFileStream.getBlockCount` work; counting rows from
block headers is what `avro-tools count` does. Records are decoded elsewhere (by Polars), never
here: this module only frames bytes, the way `csv_framer` frames CSV records before a parser.

Every length read from the file is checked before it is used: a varint is at most 10 bytes and
fits int64; a block size is checked against the bytes left in the file and an absolute cap
before anything is read; decompression has an output cap. Refusals carry positions and fixed
reason words only.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

import pyarrow as pa

from decoy_engine.io.format_errors import AvroBlockTooLarge, AvroMalformed, AvroSchemaUnsupported

MAGIC = b"Obj\x01"
SYNC_SIZE = 16
MAX_HEADER_BYTES = 16 * 1024 * 1024
# The largest block (compressed or not) any route reads. A file written as one huge block is
# read by the full route up to this size and refused above it.
MAX_BLOCK_BYTES = 256 * 1024 * 1024
_MAX_VARINT_BYTES = 10
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
CODECS = frozenset({"null", "deflate", "snappy"})
_SNAPPY_CRC_BYTES = 4


class _Truncated(AvroMalformed):
    """Internal: the source ended or a length overran its bound; mapped to a coded refusal."""


class _Bounded:
    """A forward reader over a binary stream with a hard end (file length or header cap)."""

    def __init__(self, stream: BinaryIO, end: int, start: int = 0) -> None:
        self._stream = stream
        self.pos = start
        self.end = end

    @property
    def left(self) -> int:
        return self.end - self.pos

    def read(self, n: int) -> bytes:
        if n < 0 or n > self.left:
            raise _Truncated
        data = self._stream.read(n) if n else b""
        if len(data) != n:
            raise _Truncated
        self.pos += n
        return data

    def skip(self, n: int) -> None:
        if n < 0 or n > self.left:
            raise _Truncated
        self._stream.seek(n, 1)
        self.pos += n


def read_long(src: _Bounded) -> int:
    """One zigzag varint `long`: at most 10 bytes, inside int64."""
    value = 0
    shift = 0
    for _ in range(_MAX_VARINT_BYTES):
        byte = src.read(1)[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            decoded = (value >> 1) ^ -(value & 1)
            if not _INT64_MIN <= decoded <= _INT64_MAX:
                raise _Truncated
            return decoded
        shift += 7
    raise _Truncated


def write_long(value: int) -> bytes:
    """The zigzag varint encoding of one `long`."""
    encoded = (value << 1) ^ (value >> 63)
    out = bytearray()
    while True:
        low = encoded & 0x7F
        encoded >>= 7
        if encoded:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _bytes_item(src: _Bounded) -> bytes:
    size = read_long(src)
    return src.read(size)


@dataclass(frozen=True)
class Header:
    """A parsed container header. `schema_json` is the raw `avro.schema` bytes."""

    schema_json: bytes
    codec: str
    sync: bytes
    end: int


@dataclass(frozen=True)
class Block:
    """One framed block: its object count and its (still compressed) data."""

    index: int
    count: int
    data: bytes


def _read_meta(src: _Bounded) -> dict[str, bytes]:
    meta: dict[str, bytes] = {}
    while True:
        count = read_long(src)
        if count == 0:
            return meta
        if count < 0:
            # The block form of a map: a negative count is followed by the block's byte size.
            count = -count
            size = read_long(src)
            if size < 0 or size > src.left:
                raise _Truncated
        for _ in range(count):
            entry = _bytes_item(src)
            meta[entry.decode("utf-8", "replace")] = _bytes_item(src)


def read_header(stream: BinaryIO, file_len: int, *, table: str) -> Header:
    """Parse the header from the start of `stream`; refuse anything but a well-formed one."""
    failure: str | None = None
    try:
        src = _Bounded(stream, min(file_len, MAX_HEADER_BYTES))
        if src.read(len(MAGIC)) != MAGIC:
            failure = "magic"
        else:
            meta = _read_meta(src)
            sync = src.read(SYNC_SIZE)
    except _Truncated:
        failure = "header"
    if failure is not None:
        raise AvroMalformed(table=table, reason=failure)
    schema = meta.get("avro.schema")
    if schema is None:
        raise AvroMalformed(table=table, reason="schema_missing")
    codec_raw = meta.get("avro.codec", b"null")
    codec = codec_raw.decode("ascii", "replace")
    if codec not in CODECS:
        raise AvroSchemaUnsupported(table=table, reason="codec", codec=codec[:32])
    return Header(schema_json=schema, codec=codec, sync=sync, end=src.pos)


def iter_blocks(
    stream: BinaryIO,
    header: Header,
    file_len: int,
    *,
    table: str,
    skip_data: bool = False,
) -> Iterator[Block]:
    """Frame every block after `header`; `skip_data` seeks over the data (row counting)."""
    src = _Bounded(stream, file_len, start=header.end)
    index = 0
    while src.left:
        failure: str | None = None
        too_large = False
        try:
            count = read_long(src)
            size = read_long(src)
            if count < 1 or size < 0 or count > size:
                failure = "block_header"
            elif size > MAX_BLOCK_BYTES:
                too_large = True
            elif size + SYNC_SIZE > src.left:
                failure = "block_truncated"
            else:
                if skip_data:
                    src.skip(size)
                    data = b""
                else:
                    data = src.read(size)
                if src.read(SYNC_SIZE) != header.sync:
                    failure = "sync"
        except _Truncated:
            failure = "block_truncated"
        if too_large:
            raise AvroBlockTooLarge(table=table, block=index, limit="absolute")
        if failure is not None:
            raise AvroMalformed(table=table, reason=failure, block=index)
        yield Block(index, count, data)
        index += 1


def decompress(block: Block, codec: str, cap: int, *, table: str) -> bytes:
    """The block's uncompressed bytes, refusing more than `cap` before allocating them."""
    if codec == "null":
        if len(block.data) > cap:
            raise AvroBlockTooLarge(table=table, block=block.index, limit="budget")
        return block.data
    if codec == "deflate":
        return _inflate(block, cap, table=table)
    return _unsnappy(block, cap, table=table)


def _inflate(block: Block, cap: int, *, table: str) -> bytes:
    failure: str | None = None
    too_large = False
    out = b""
    try:
        engine = zlib.decompressobj(-15)
        out = engine.decompress(block.data, cap + 1)
        if len(out) > cap:
            too_large = True
        elif engine.unconsumed_tail or engine.unused_data or not engine.eof:
            failure = "deflate"
    except zlib.error:
        failure = "deflate"
    if too_large:
        raise AvroBlockTooLarge(table=table, block=block.index, limit="budget")
    if failure is not None:
        raise AvroMalformed(table=table, reason=failure, block=block.index)
    return out


def _snappy_length(data: bytes) -> int:
    # The raw snappy preamble is an unsigned little-endian base-128 varint (at most 5 bytes).
    value = 0
    for shift, byte in zip(range(0, 35, 7), data[:5], strict=False):
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
    raise _Truncated


def _unsnappy(block: Block, cap: int, *, table: str) -> bytes:
    failure: str | None = None
    too_large = False
    out = b""
    data, crc = block.data[:-_SNAPPY_CRC_BYTES], block.data[-_SNAPPY_CRC_BYTES:]
    try:
        if len(block.data) <= _SNAPPY_CRC_BYTES:
            raise _Truncated
        length = _snappy_length(data)
        if length > cap:
            too_large = True
        else:
            out = pa.Codec("snappy").decompress(data, decompressed_size=length, asbytes=True)
            if len(out) != length or zlib.crc32(out).to_bytes(4, "big") != crc:
                failure = "snappy"
    except (_Truncated, pa.ArrowException):
        failure = "snappy"
    if too_large:
        raise AvroBlockTooLarge(table=table, block=block.index, limit="budget")
    if failure is not None:
        raise AvroMalformed(table=table, reason=failure, block=block.index)
    return out


def encode_header(schema_json: bytes, sync: bytes, codec: str = "null") -> bytes:
    """A container header holding exactly `avro.schema` and `avro.codec`."""
    out = bytearray(MAGIC)
    out += write_long(2)
    for key, value in ((b"avro.schema", schema_json), (b"avro.codec", codec.encode("ascii"))):
        out += write_long(len(key)) + key + write_long(len(value)) + value
    out += write_long(0)
    out += sync
    return bytes(out)


def encode_block(count: int, data: bytes, sync: bytes) -> bytes:
    return write_long(count) + write_long(len(data)) + data + sync


__all__ = [
    "CODECS",
    "MAGIC",
    "MAX_BLOCK_BYTES",
    "MAX_HEADER_BYTES",
    "SYNC_SIZE",
    "Block",
    "Header",
    "decompress",
    "encode_block",
    "encode_header",
    "iter_blocks",
    "read_header",
    "read_long",
    "write_long",
]

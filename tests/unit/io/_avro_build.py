"""Hand-built Avro object container files, per the Avro specification's binary encoding, so
the reader is tested against bytes no engine code produced."""

from __future__ import annotations

import json
import zlib
from typing import Any

import pyarrow as pa

SYNC = bytes(range(16))


def zz(n: int) -> bytes:
    """Zigzag varint `long`."""
    n = (n << 1) ^ (n >> 63)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def astr(b: bytes) -> bytes:
    return zz(len(b)) + b


def header(schema: Any, codec: bytes | None = b"null", sync: bytes = SYNC) -> bytes:
    raw = schema if isinstance(schema, bytes) else json.dumps(schema).encode()
    entries = [(b"avro.schema", raw)] + ([(b"avro.codec", codec)] if codec is not None else [])
    meta = zz(len(entries)) + b"".join(astr(k) + astr(v) for k, v in entries) + zz(0)
    return b"Obj\x01" + meta + sync


def block(count: int, data: bytes, sync: bytes = SYNC) -> bytes:
    return zz(count) + zz(len(data)) + data + sync


def container(
    schema: Any, blocks: list[tuple[int, bytes]], codec: bytes = b"null", sync: bytes = SYNC
) -> bytes:
    return header(schema, codec, sync) + b"".join(block(c, d, sync) for c, d in blocks)


def deflate(data: bytes) -> bytes:
    c = zlib.compressobj(wbits=-15)
    return c.compress(data) + c.flush()


def snappy(data: bytes) -> bytes:
    return pa.Codec("snappy").compress(data, asbytes=True) + zlib.crc32(data).to_bytes(4, "big")


def record(fields: list[tuple[str, Any]], name: str = "r") -> dict[str, Any]:
    return {"type": "record", "name": name, "fields": [{"name": n, "type": t} for n, t in fields]}


LONG_SCHEMA = record([("x", "long"), ("s", "string")])


def long_rows(start: int, n: int) -> bytes:
    return b"".join(zz(i) + astr(f"v{i}".encode()) for i in range(start, start + n))


def long_file(blocks: int = 3, per_block: int = 4, codec: bytes = b"null") -> bytes:
    enc = {b"null": lambda d: d, b"deflate": deflate, b"snappy": snappy}[codec]
    return container(
        LONG_SCHEMA,
        [(per_block, enc(long_rows(b * per_block, per_block))) for b in range(blocks)],
        codec=codec,
    )

"""Typed, length-framed digest codec for the faker pool determinism harness
(plan_faker_determinism_harness_v2.md, C1).

`repr()` is not a codec: it is locale- and implementation-defined for some
types, collapses distinct values onto the same text for others (`1` vs
`True` under a loose comparison, though `repr` itself tells those apart --
the real hazard is a value whose `repr` isn't stably reproducible, or a type
`repr` doesn't roundtrip at all), and gives no way to prove two DIFFERENT
Python types never collide onto the same bytes. `_pool_digest` instead
frames every item by an explicit type tag and byte length, so the only way
two distinct pools produce the same encoding is a real SHA-256 collision.

Wire format (all integers big-endian, unsigned unless noted):

    codec_version (1 byte)
    count         (8 bytes, u64)
    for each item, in order:
        type_tag  (1 byte)
        length    (8 bytes, u64)
        payload   (`length` bytes)

Per-type payload:
    NONE  -- empty.
    BOOL  -- one byte, 0x00 or 0x01.
    INT   -- one sign byte (0x00 non-negative, 0x01 negative) followed by
             the minimal big-endian magnitude (zero magnitude bytes for 0).
    FLOAT -- 8 bytes, IEEE-754 big-endian (`struct.pack(">d", ...)`), so
             NaN and the two signed zeros round-trip through the SAME bit
             pattern Python's own float storage uses -- no exact-decimal
             reformatting to gain or lose.
    STR   -- UTF-8 bytes of the string AS GIVEN. No Unicode normalization:
             composed and decomposed forms of the same visible character
             have different UTF-8 bytes and must digest to different
             values, because two Faker providers that emit different
             normalization forms are not interchangeable pool entries.
    BYTES -- the raw bytes, unchanged.

`bool` is checked before `int` (`isinstance(True, int)` is `True` in
Python; encoding a bool through the INT branch would collide `True`/`1` and
`False`/`0` onto identical bytes, which the DISTINCT-tag requirement below
forbids).

Any other type raises `PoolDigestTypeError` -- pool values are Faker
provider return types (str, and the handful of scalar Python types Faker's
non-str providers return), never something this codec cannot express.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Any

CODEC_VERSION = 1

_TAG_NONE = 0x00
_TAG_BOOL = 0x01
_TAG_INT = 0x02
_TAG_FLOAT = 0x03
_TAG_STR = 0x04
_TAG_BYTES = 0x05

_U64_STRUCT = struct.Struct(">Q")
_F64_STRUCT = struct.Struct(">d")


class PoolDigestTypeError(TypeError):
    """A pool value's type is not one `_pool_digest` can encode."""


def _encode_int(value: int) -> bytes:
    sign = b"\x01" if value < 0 else b"\x00"
    magnitude = abs(value)
    nbytes = (magnitude.bit_length() + 7) // 8
    return sign + magnitude.to_bytes(nbytes, "big", signed=False)


def _encode_item(index: int, value: Any) -> tuple[int, bytes]:
    """Return `(type_tag, payload)` for one pool value, or raise
    `PoolDigestTypeError` naming the offending index and type."""
    if value is None:
        return _TAG_NONE, b""
    if type(value) is bool:  # must precede the int check: isinstance(True, int) is True
        return _TAG_BOOL, (b"\x01" if value else b"\x00")
    if isinstance(value, int):
        return _TAG_INT, _encode_int(value)
    if isinstance(value, float):
        return _TAG_FLOAT, _F64_STRUCT.pack(value)
    if isinstance(value, str):
        return _TAG_STR, value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return _TAG_BYTES, bytes(value)
    raise PoolDigestTypeError(
        f"pool value at index {index} has unsupported type {type(value).__name__!r} "
        f"({value!r}); _pool_digest only encodes None/bool/int/float/str/bytes"
    )


def _pool_digest(values: Sequence[Any]) -> bytes:
    """Encode `values` into the versioned, length-framed typed codec this
    module documents. Deterministic and order-sensitive: same values in a
    different order encode to different bytes. Callers take
    `hashlib.sha256(_pool_digest(values)).digest()` for a fixed-size digest;
    this function returns the full pre-hash encoding so a caller that wants
    the raw framed bytes (tests, debugging) never has to reverse a hash.
    """
    parts: list[bytes] = [bytes([CODEC_VERSION]), _U64_STRUCT.pack(len(values))]
    for index, value in enumerate(values):
        type_tag, payload = _encode_item(index, value)
        parts.append(bytes([type_tag]))
        parts.append(_U64_STRUCT.pack(len(payload)))
        parts.append(payload)
    return b"".join(parts)


__all__ = [
    "CODEC_VERSION",
    "PoolDigestTypeError",
    "_pool_digest",
]

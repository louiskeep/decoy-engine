"""Source-snapshot digest codec for the native route's widened admission.

Split out of `_native_route_preflight.py` so that module keeps headroom under
the orchestration-size cap: this holds the self-contained digest unit (the
four-state column resolver the accumulator reports through, the domain-
separated hasher framing, the per-array/per-column/whole-source codec, and the
`PreflightColumnAccumulator` that folds a column batch by batch). Nothing here
imports back from `_native_route_preflight`/`_native_route_exec`; the
dependency runs one way (preflight/exec import this).

The digest exists because `LazySource` reopens the file on every read, so a
footer/row-count check alone cannot pin the preflight read and the execution
read to the same bytes. Both passes fold the identical byte streams here; a
mismatch after the second pass drains aborts the commit -- see
`_native_route_preflight.ExecutionDigestState.verify`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

ColumnState = Literal["empty", "no_null", "partial_null", "all_null"]


def resolve_column_state(total_rows: int, null_count: int) -> ColumnState:
    """One column's global state from its accumulated counts (plan section 2).

    The four states are disjoint and exhaustive over `0 <= null_count <=
    total_rows`: `empty` short-circuits on row count alone (a zero-row
    column has no nulls to count), so the remaining three only apply to a
    non-empty column.
    """
    if total_rows == 0:
        return "empty"
    if null_count == 0:
        return "no_null"
    if null_count == total_rows:
        return "all_null"
    return "partial_null"


# A fixed, readable domain-separation constant (blake2b `key=`, <=64 bytes)
# plus an explicit version byte folded into every hasher -- so a future
# codec revision cannot collide with this one even if the framing bytes
# happen to overlap.
_DOMAIN_KEY = b"decoy-engine/native-route/source-snapshot-digest/v1"
_VERSION_BYTE = b"\x01"
_DIGEST_SIZE = 32


def _new_hasher() -> Any:
    h = hashlib.blake2b(digest_size=_DIGEST_SIZE, key=_DOMAIN_KEY)
    h.update(_VERSION_BYTE)
    return h


def _type_token(arrow_type: pa.DataType) -> str:
    """A short, unambiguous string identifying `arrow_type` for the digest
    header: unit + timezone for timestamp, signedness + width for integer,
    so a tz change or a width change changes the digest even if every value
    happens to coincide numerically."""
    if arrow_type == pa.utf8():
        return "utf8"
    if pa.types.is_boolean(arrow_type):
        return "bool"
    if pa.types.is_integer(arrow_type):
        sign = "i" if pa.types.is_signed_integer(arrow_type) else "u"
        return f"{sign}{arrow_type.bit_width}"
    if pa.types.is_timestamp(arrow_type):
        return f"ts:{arrow_type.unit}:{arrow_type.tz or ''}"
    raise AssertionError(
        f"no digest type token for {arrow_type!s}"
    )  # pragma: no cover - admitted-types-only


def _update_hashers_for_array(
    validity_hasher: Any, aux_hasher: Any, value_hasher: Any, array: pa.Array
) -> None:
    """Fold one batch of one column into THREE separate hashers so re-
    partitioning the same data into different batch sizes folds to
    identical byte streams (plan section 4's partition-independence). Each
    quantity gets its OWN stream (never interleaved with another per batch
    -- validity-then-values or lengths-then-data interleaved once per batch
    is NOT partition-independent: an N-batch run interleaves N times, a
    1-batch run once, even though each quantity's own stream alone is).
    `aux_hasher` carries utf8's per-row length stream (untouched for every
    other type); `value_hasher` carries the payload (utf8 raw bytes, or the
    fixed-width numeric/bool/timestamp-ticks values).

    Validity is one byte per row (`pc.is_valid`, not a packed bitmap): no
    padding-bit bookkeeping across a batch boundary, at the cost of 7
    wasted bits per row. Every branch reads through a pyarrow compute
    kernel (`fill_null`, `is_valid`, `to_numpy`, a cast), so `.offset` is
    always honored by pyarrow's own conversion, never re-derived by hand. A
    null-free batch skips `is_valid`/`fill_null` (their result would equal
    an all-true validity string and the unchanged array): measured to
    matter, since this runs on every admitted batch twice and most real
    columns are null-free.
    """
    no_nulls = array.null_count == 0
    if no_nulls:
        validity_hasher.update(b"\x01" * len(array))
    else:
        validity = pc.is_valid(array)  # type: ignore[attr-defined, unused-ignore]
        validity_hasher.update(validity.to_numpy(zero_copy_only=False).tobytes())
    arrow_type = array.type
    if arrow_type == pa.utf8():
        # Null payloads folded to "" (zero-length, so a null contributes
        # nothing beyond its already-hashed validity byte); lengths go to
        # their OWN stream so the value bytes are length-PREFIXED, not a
        # bare concatenation -- "ab"+"c" and "a"+"bc" must never collide.
        filled = array if no_nulls else pc.fill_null(array, "")
        n = len(filled)
        offsets_buf = filled.buffers()[1]
        offsets = np.frombuffer(offsets_buf, dtype=np.int32, count=n + 1, offset=filled.offset * 4)
        aux_hasher.update(np.diff(offsets).astype(">i4").tobytes())
        data_buf = filled.buffers()[2]
        start, end = int(offsets[0]), int(offsets[-1])
        if data_buf is not None and end > start:
            value_hasher.update(memoryview(data_buf)[start:end])
        return
    if pa.types.is_boolean(arrow_type):
        filled_bool = array if no_nulls else pc.fill_null(array, False)
        value_hasher.update(filled_bool.to_numpy(zero_copy_only=False).tobytes())
        return
    if pa.types.is_integer(arrow_type):
        filled_int = array if no_nulls else pc.fill_null(array, 0)
        value_hasher.update(filled_int.to_numpy(zero_copy_only=False).tobytes())
        return
    if pa.types.is_timestamp(arrow_type):
        filled_ts = array if no_nulls else pc.fill_null(array, pa.scalar(0, type=arrow_type))
        value_hasher.update(filled_ts.cast(pa.int64()).to_numpy(zero_copy_only=False).tobytes())
        return
    raise AssertionError(
        f"no digest encoder for {arrow_type!s}"
    )  # pragma: no cover - admitted-types-only


def _finalize_column_digest(
    validity_hasher: Any,
    aux_hasher: Any,
    value_hasher: Any,
    *,
    name: str,
    arrow_type: pa.DataType,
    total_rows: int,
) -> bytes:
    """One column's digest, framed ONCE (name, type token, row count) around
    the three whole-column hashes folded across every batch -- so a
    reordered schema, a renamed column, or a row-count mismatch changes the
    digest even if two columns' value streams happen to coincide."""
    framed = _new_hasher()
    name_bytes = name.encode("utf-8")
    framed.update(len(name_bytes).to_bytes(4, "big"))
    framed.update(name_bytes)
    token_bytes = _type_token(arrow_type).encode("utf-8")
    framed.update(len(token_bytes).to_bytes(4, "big"))
    framed.update(token_bytes)
    framed.update(total_rows.to_bytes(8, "big"))
    framed.update(validity_hasher.digest())
    framed.update(aux_hasher.digest())
    framed.update(value_hasher.digest())
    return framed.digest()


def combine_column_digests(column_digests: list[bytes]) -> bytes:
    """The whole-source digest: the frozen column ORDER folded in by simply
    updating in that order, so a reordered schema changes the result."""
    top = _new_hasher()
    for digest in column_digests:
        top.update(digest)
    return top.digest()


@dataclass
class PreflightColumnAccumulator:
    """Per-column running state for one pass: row/null counts for the
    four-state resolver, plus the three digest hashers folded batch by
    batch. O(1) memory per batch; nothing retains an array reference."""

    name: str
    arrow_type: pa.DataType
    total_rows: int = 0
    null_count: int = 0
    _validity_hasher: Any = field(default_factory=_new_hasher, repr=False)
    _aux_hasher: Any = field(default_factory=_new_hasher, repr=False)
    _value_hasher: Any = field(default_factory=_new_hasher, repr=False)

    def observe(self, array: pa.Array) -> None:
        self.total_rows += len(array)
        self.null_count += array.null_count
        _update_hashers_for_array(
            self._validity_hasher, self._aux_hasher, self._value_hasher, array
        )

    def state(self) -> ColumnState:
        return resolve_column_state(self.total_rows, self.null_count)

    def digest(self) -> bytes:
        return _finalize_column_digest(
            self._validity_hasher,
            self._aux_hasher,
            self._value_hasher,
            name=self.name,
            arrow_type=self.arrow_type,
            total_rows=self.total_rows,
        )


__all__ = [
    "ColumnState",
    "PreflightColumnAccumulator",
    "combine_column_digests",
    "resolve_column_state",
]

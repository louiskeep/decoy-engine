"""Arrow type predicates and the exact key cast, shared across layers (bottom leaf; imports
only pyarrow).

`string` and `large_string` are the same logical UTF-8 type with 32-bit or 64-bit offsets, and
a parquet read keeps whichever the file declared, so every check for "text" must accept both.

`exact_key_cast` is the cast the out-of-core FK join uses to unify one output column's chunk
types (`cast_fk_chunk`). It is not a matching rule: which key values are equal is decided by the
join tokens in `decoy_engine.internal.fk_tokens`.
"""

from __future__ import annotations

import pyarrow as pa


def is_text(t: pa.DataType) -> bool:
    """True for `string` and `large_string`."""
    return pa.types.is_string(t) or pa.types.is_large_string(t)


def is_text_or_null(t: pa.DataType) -> bool:
    """`is_text`, or the `null` type of an all-null batch the reader could not type."""
    return is_text(t) or pa.types.is_null(t)


def exact_key_cast(chunk: pa.Array, target: pa.DataType) -> pa.Array:
    """Cast one key chunk to `target` without silent drift, or raise an Arrow error.

    pyarrow's safe cast rejects every int -> float conversion outside [-2**53, 2**53], even a
    value that is exactly representable, because it guards the whole doubtful range. An integer
    chunk is therefore widened unsafely and kept only when it casts straight back to the same
    integers; a key that would round raises `ArrowInvalid`. The error carries the types only:
    pyarrow's own text quotes the value, and that text is dropped.
    """
    if chunk.type.equals(target):
        return chunk
    if pa.types.is_integer(chunk.type) and pa.types.is_floating(target):
        widened = chunk.cast(target, safe=False)
        if not widened.cast(chunk.type, safe=False).equals(chunk):
            raise pa.ArrowInvalid(f"cannot cast a key chunk {chunk.type} -> {target} exactly")
        return widened
    try:
        return chunk.cast(target)
    except MemoryError:
        raise
    except pa.ArrowException as exc:
        failed = type(exc)
    raise failed(f"cannot cast a key chunk {chunk.type} -> {target} exactly")

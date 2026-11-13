"""Arrow type predicates and the one exact key cast, shared across layers (bottom leaf; imports
only pyarrow).

`string` and `large_string` are the same logical UTF-8 type with 32-bit or 64-bit offsets, and
a parquet read keeps whichever the file declared, so every check for "text" must accept both.

`exact_key_cast` is the one cast that turns one FK key column into another type. The FK join
routes reach it through `cast_fk_chunk`, and the post-scan key counts through
`key_comparison_type`, so a pair the join can bring together is a pair the gate compares.
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


def key_comparison_type(parent: pa.DataType, child: pa.DataType) -> pa.DataType | None:
    """The type a parent key end and a child key end are compared in, or `None` when
    `exact_key_cast` has no route between them. Reads types only. A dictionary compares as its
    values; a null type is comparable with everything and takes the other side's type. The
    parent's type wins when the child casts to it, since the parent is the side that was written
    first."""
    if pa.types.is_dictionary(parent):
        parent = parent.value_type
    if pa.types.is_dictionary(child):
        child = child.value_type
    if pa.types.is_null(parent):
        return child
    if pa.types.is_null(child) or parent.equals(child):
        return parent
    order = ((child, parent), (parent, child))
    if _total_target(parent, child) is child:
        order = order[::-1]
    for source, target in order:
        try:
            exact_key_cast(pa.array([], type=source), target)
        except pa.ArrowException:
            continue
        return target
    return None


def _total_target(a: pa.DataType, b: pa.DataType) -> pa.DataType | None:
    """The one of two types the other always casts into: binary holds any text, text holds any
    integer, and a float holds the integers it can (a few huge ones are refused by the cast).
    Comparing in that type keeps a value with no counterpart an orphan, as the join counts it,
    instead of a cast failure. `None` when neither side is the wider one."""
    for wide, narrow in ((a, b), (b, a)):
        if pa.types.is_binary(wide) or pa.types.is_large_binary(wide):
            if is_text(narrow):
                return wide
        elif (is_text(wide) and not (is_text(narrow) or pa.types.is_null(narrow))) or (
            pa.types.is_floating(wide) and pa.types.is_integer(narrow)
        ):
            return wide
    return None

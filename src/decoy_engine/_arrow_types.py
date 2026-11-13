"""Arrow type predicates shared across layers (bottom leaf; imports only pyarrow).

`string` and `large_string` are the same logical UTF-8 type with 32-bit or 64-bit offsets, and
a parquet read keeps whichever the file declared, so every check for "text" must accept both.
"""

from __future__ import annotations

import pyarrow as pa


def is_text(t: pa.DataType) -> bool:
    """True for `string` and `large_string`."""
    return pa.types.is_string(t) or pa.types.is_large_string(t)


def is_text_or_null(t: pa.DataType) -> bool:
    """`is_text`, or the `null` type of an all-null batch the reader could not type."""
    return is_text(t) or pa.types.is_null(t)

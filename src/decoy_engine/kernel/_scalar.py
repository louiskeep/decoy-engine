"""Arrow-array scalar masking kernels for deterministic per-value strategies."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.determinism import derive
from decoy_engine.kernel._canonicalize import canonicalize_derive_source


def _is_missing(value: Any) -> bool:
    """True for a value the masking kernels must treat as missing (null).

    None and IEEE NaN both count, matching pandas ``isna()`` and the
    ``pa.array(..., from_pandas=True)`` conversion the full-frame pandas path
    runs BEFORE a value ever reaches these kernels (that conversion folds NaN
    to null). The out-of-core route feeds raw Arrow values straight in, so a
    float column carrying an actual NaN would otherwise be hashed / redacted /
    stringified ("nan") here where the oracle emitted null. Only NaN-like
    values are unequal to themselves, so ``value != value`` detects float,
    numpy-float, and Decimal('NaN') alike without special-casing each type; a
    non-comparable object simply is not missing.
    """
    if value is None:
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def _array_to_pylist(values: pa.Array | pa.ChunkedArray | list[Any]) -> list[Any]:
    """Normalize the kernel's input to a plain Python list of scalars.

    A real Arrow array/chunked-array is the common (fast) path. A caller may
    also pass a plain list of raw Python scalars: mixed-type pandas object
    columns (str and int values in one column) have no single Arrow type, so
    `pa.array(..., from_pandas=True)` can raise before ever reaching the
    kernel; the caller falls back to a raw Python list in that case (see
    `_hash.py`/`_truncate.py`) instead of duplicating the per-value logic
    below. Every kernel here is already per-value dispatch (canonicalize by
    Python type, or `str(value)`), so operating on raw scalars is no
    different from operating on `to_pylist()` output.
    """
    if isinstance(values, pa.ChunkedArray):
        return values.combine_chunks().to_pylist()
    if isinstance(values, pa.Array):
        return values.to_pylist()
    return list(values)


def passthrough_array(values: pa.Array | pa.ChunkedArray) -> pa.Array:
    """Return values as one Arrow array without changing logical values."""
    if isinstance(values, pa.ChunkedArray):
        return values.combine_chunks()
    return values


def hash_array(
    values: pa.Array | pa.ChunkedArray | list[Any],
    *,
    seed: bytes,
    namespace: str,
    truncate: int | None = None,
    derive_func=derive,
) -> pa.Array:
    """Mask non-null values with derive(seed, namespace, canonical(value)).hex()."""
    out: list[str | None] = []
    for value in _array_to_pylist(values):
        if _is_missing(value):
            out.append(None)
            continue
        token = derive_func(seed, namespace, canonicalize_derive_source(value)).hex()
        out.append(token[:truncate] if truncate is not None else token)
    return pa.array(out, type=pa.string())


def redact_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    redact_with: Any = "REDACTED",
) -> pa.Array:
    """Replace every non-null value with ``redact_with``."""
    return pa.array(
        [None if _is_missing(value) else redact_with for value in _array_to_pylist(values)]
    )


def truncate_array(
    values: pa.Array | pa.ChunkedArray | list[Any],
    *,
    length: int,
    keep: str = "head",
    mask_char: str | None = None,
) -> pa.Array:
    """Apply the truncate strategy to non-null values."""
    out: list[str | None] = []
    for value in _array_to_pylist(values):
        if _is_missing(value):
            out.append(None)
            continue
        text = str(value)
        if mask_char is None:
            out.append(text[-length:] if keep == "tail" else text[:length])
        elif keep == "tail":
            keep_part = text[-length:]
            drop_part = text[:-length] if length < len(text) else ""
            out.append((mask_char * len(drop_part)) + keep_part)
        else:
            keep_part = text[:length]
            drop_part = text[length:] if length < len(text) else ""
            out.append(keep_part + (mask_char * len(drop_part)))
    return pa.array(out, type=pa.string())

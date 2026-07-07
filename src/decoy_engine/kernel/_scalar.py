"""Arrow-array scalar masking kernels for deterministic per-value strategies."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.determinism import derive
from decoy_engine.kernel._canonicalize import canonicalize_derive_source


def _array_to_pylist(values: pa.Array | pa.ChunkedArray) -> list[Any]:
    if isinstance(values, pa.ChunkedArray):
        return values.combine_chunks().to_pylist()
    return values.to_pylist()


def passthrough_array(values: pa.Array | pa.ChunkedArray) -> pa.Array:
    """Return values as one Arrow array without changing logical values."""
    if isinstance(values, pa.ChunkedArray):
        return values.combine_chunks()
    return values


def hash_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    seed: bytes,
    namespace: str,
    truncate: int | None = None,
    derive_func=derive,
) -> pa.Array:
    """Mask non-null values with derive(seed, namespace, canonical(value)).hex()."""
    out: list[str | None] = []
    for value in _array_to_pylist(values):
        if value is None:
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
    return pa.array([None if value is None else redact_with for value in _array_to_pylist(values)])


def truncate_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    length: int,
    keep: str = "head",
    mask_char: str | None = None,
) -> pa.Array:
    """Apply the truncate strategy to non-null values."""
    out: list[str | None] = []
    for value in _array_to_pylist(values):
        if value is None:
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

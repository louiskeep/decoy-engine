"""Backend-neutral deterministic masking kernel.

The kernel is the shared Arrow boundary for deterministic per-value masking
logic. It does not define FK equality semantics; those live in
``decoy_engine.execution._fk_keys``.
"""

from decoy_engine.kernel._canonicalize import canonicalize_derive_source, encode_int
from decoy_engine.kernel._scalar import (
    hash_array,
    passthrough_array,
    redact_array,
    truncate_array,
)

__all__ = [
    "canonicalize_derive_source",
    "encode_int",
    "hash_array",
    "passthrough_array",
    "redact_array",
    "truncate_array",
]

"""Derive-input canonicalization for the masking kernel.

This module intentionally re-exports the existing generation canonicalizer. The
byte rules are part of the determinism envelope; moving the import path must not
change output bytes or FK match behavior.
"""

from decoy_engine.generation.pool._canonicalize import (
    _canonicalize_source as canonicalize_derive_source,
)
from decoy_engine.generation.pool._canonicalize import _encode_int as encode_int

__all__ = ["canonicalize_derive_source", "encode_int"]

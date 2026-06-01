"""Crypto / hash primitives used internally by the engine (V2.0-C).

Split out of the bundled internal/helpers.py. All functions are
deterministic and intended for use by the masking / FK / generator
pipelines, not by callers outside the engine -- the public API for
keyed primitives lives in ``decoy_engine`` itself (HKDF via the key
resolver, etc.).
"""

from __future__ import annotations

import hashlib
import hmac
import warnings
from typing import Any


def deterministic_hash(value: Any, seed: int = 0) -> str | None:
    """Legacy SHA256(value + seed) hash. Kept for backwards compatibility
    when no master key is configured. Prefer :func:`hmac_hex` (keyed) for
    any new code path so output is per-tenant and not derivable from the
    value alone.

    QA-internal-synth-providers F12 (2026-06-01, LOW security):
    emits a DeprecationWarning so accidental callers (e.g. a new
    masking strategy copied from an older one) surface in CI tools
    that treat warnings as errors. The docstring already said
    "Prefer hmac_hex"; this makes the deprecation machine-readable.
    """
    warnings.warn(
        "deterministic_hash uses SHA256(value+seed) which is reversible "
        "given the seed. Use hmac_hex for all new code.",
        DeprecationWarning,
        stacklevel=2,
    )
    if value is None:
        return None
    value_str = f"{value}{seed}"
    hash_obj = hashlib.sha256(value_str.encode())
    return hash_obj.hexdigest()


def hmac_hex(key: bytes, value: Any) -> str | None:
    """HMAC-SHA256(key, value) as a 64-char hex string.

    The "Path B" deterministic primitive: same key + same input always
    yields the same output, with no per-tenant secret leakage (unlike
    SHA256(value + seed) where the seed is recoverable by brute force on
    a single known mapping).
    """
    if value is None:
        return None
    msg = str(value).encode("utf-8", errors="replace")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def hmac_seed(key: bytes, value: Any) -> int:
    """Derive a 32-bit integer seed for Faker.seed_instance(...) from
    HMAC-SHA256(key, value). Same input + same key -> same seed -> same
    Faker output, with zero state stored anywhere.
    """
    if value is None:
        return 0
    msg = str(value).encode("utf-8", errors="replace")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    return int.from_bytes(digest[:4], "big")

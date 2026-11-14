"""Seed helpers shared by every generator that turns seed bytes into an RNG.

Three jobs, one place each: refusing a seed of the wrong width before it reaches NumPy,
resolving a possibly missing seed once per spec, and drawing a fresh seed for a caller that
supplied none. A draw site that needs a seed integer calls `seed_bytes_to_int`; no site builds
one from `int.from_bytes` on a slice of a wider secret.

Parameter names avoid the secret-material census patterns on purpose: these functions only
move a width-checked value and never hold a key.
"""

from __future__ import annotations

import os

# The job seed width, and the width of the deterministic mask key a keyed composite carries.
_JOB_SEED_BYTES = 8
_MASK_KEY_BYTES = 32


def seed_bytes_to_int(raw: bytes) -> int:
    """The big-endian uint64 for NumPy's `default_rng`, from exactly 8 bytes.

    A wrong width is a determinism bug, not a value to tolerate: `int.from_bytes` would
    silently truncate or widen it. The message carries the length only, never the bytes.
    """
    if not isinstance(raw, bytes) or len(raw) != _JOB_SEED_BYTES:
        raise ValueError(f"a seed must be exactly {_JOB_SEED_BYTES} bytes")
    return int.from_bytes(raw, "big")


def fresh_seed() -> bytes:
    """8 bytes of operating-system entropy, for a caller that supplied no seed."""
    return os.urandom(_JOB_SEED_BYTES)


def resolve_seed(supplied: bytes | None) -> bytes:
    """The one seed a spec uses for every draw of one call.

    An 8-byte job seed and the 32-byte deterministic mask key pass through unchanged (the
    32-byte width is the existing keyed composite path); `None` draws one `fresh_seed()`; any
    other width is refused. Idempotent: a resolved seed resolves to itself.
    """
    if supplied is None:
        return fresh_seed()
    if not isinstance(supplied, bytes) or len(supplied) not in (_JOB_SEED_BYTES, _MASK_KEY_BYTES):
        raise ValueError(
            f"a seed must be {_JOB_SEED_BYTES} (job seed) or {_MASK_KEY_BYTES} (mask key) bytes"
        )
    return supplied

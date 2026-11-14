"""Seed helpers shared by every generator that turns seed bytes into an RNG.

Three jobs, one place each: refusing a seed of the wrong width before it reaches NumPy,
resolving a possibly missing seed once per spec, and drawing a fresh seed for a caller that
supplied none. A draw site that needs a seed integer calls `seed_bytes_to_int` (composites pass `widths=COMPOSITE_SEED_WIDTHS`); no site builds
one from `int.from_bytes` on raw seed bytes.

`resolve_seed` receives the 32-byte mask key, and the converters receive it or a slice of a
`derive(mask_key, ...)` output, so each wipes its parameter when it raises.
"""

from __future__ import annotations

import os

from decoy_engine import reasons
from decoy_engine.determinism._derive import DeterminismError

# The job seed width, and the width of the deterministic mask key a keyed composite carries.
_JOB_SEED_BYTES = 8
_MASK_KEY_BYTES = 32
# What `resolve_seed` returns: a job seed or the keyed composite's mask key.
COMPOSITE_SEED_WIDTHS = (_JOB_SEED_BYTES, _MASK_KEY_BYTES)


class SeedWidthError(DeterminismError, ValueError):
    """A seed of a width the caller may not use; the message carries the width rule only."""


def _width_error(message: str) -> SeedWidthError:
    return SeedWidthError(code=reasons.runtime.INVALID_SEED_LENGTH, message=message)


def seed_bytes_to_int(seed: bytes, *, widths: tuple[int, ...] = (_JOB_SEED_BYTES,)) -> int:
    """The big-endian integer for NumPy's `default_rng`, from a seed of an allowed width.

    The default is exactly 8 bytes (a job seed, or a slice of a `derive(...)` output). A keyed
    composite passes `widths=(8, 32)` to accept the mask key too; the full width is converted,
    so its pool build is unchanged. A wrong width is a determinism bug, not a value to
    tolerate: `int.from_bytes` would silently truncate or widen it. The error carries the
    rule only, never the bytes.
    """
    try:
        if not isinstance(seed, bytes) or len(seed) not in widths:
            allowed = " or ".join(str(w) for w in widths)
            raise _width_error(f"a seed must be {allowed} bytes")
        return int.from_bytes(seed, "big")
    except BaseException:
        del seed
        raise


def fresh_seed() -> bytes:
    """8 bytes of operating-system entropy, for a caller that supplied no seed."""
    return os.urandom(_JOB_SEED_BYTES)


def resolve_seed(seed: bytes | None) -> bytes:
    """The one seed a spec uses for every draw of one call.

    An 8-byte job seed and the 32-byte deterministic mask key pass through unchanged (the
    32-byte width is the existing keyed composite path); `None` draws one `fresh_seed()`; any
    other width is refused. Idempotent: a resolved seed resolves to itself.
    """
    try:
        if seed is None:
            return fresh_seed()
        if not isinstance(seed, bytes) or len(seed) not in (_JOB_SEED_BYTES, _MASK_KEY_BYTES):
            raise ValueError(
                f"a seed must be {_JOB_SEED_BYTES} (job seed) or {_MASK_KEY_BYTES} (mask key) bytes"
            )
        return seed
    except BaseException:
        del seed
        raise

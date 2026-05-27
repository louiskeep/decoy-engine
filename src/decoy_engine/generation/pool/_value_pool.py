"""ValuePool: frozen, deterministically-built array of synthetic values + identity.

Built by PoolBuilder, sampled by PoolSampler, cached by PoolCache. Frozen
because mutating a pool mid-job would shift sampling output silently
(no-silent-shift contract per the operating model).

Per S5 spec §2, the **identity tuple** is `(provider, locale, config_hash,
seed, size)`. Five fields. NOT `backend_version` (a Faker patch shifting
output produces a different pool, but caching on backend_version would
force every job to rebuild on every Faker patch; the manifest records
the version separately for audit).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class ValuePool:
    """Frozen synthetic-value array with identity metadata.

    Per S5 spec §2: numpy `setflags(write=False)` on `values` after build
    so a buggy strategy that tries to sort in-place fails loudly rather
    than silently re-keying the deterministic sampler.

    `backend_type` + `backend_version` are sourced from
    `ProviderRegistry.get_capabilities(provider)` (per S4's H1 path),
    NOT from the adapter directly.
    """

    values: np.ndarray[Any, Any]
    provider: str
    locale: str
    config_hash: str
    seed: bytes
    size: int
    build_time_ms: float
    backend_type: str
    backend_version: str
    distinct_count: int

    @property
    def identity(self) -> tuple[str, str, str, bytes, int]:
        """The cache key per S5 spec §2 + cross-sprint contracts §2.5."""
        return (self.provider, self.locale, self.config_hash, self.seed, self.size)


def _freeze_array(values: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """Mark a numpy array read-only after build.

    Per S5 spec §2 "Why frozen": a strategy that sorts in place for
    binary search would re-key the deterministic sampler. setflags
    makes the attempt raise rather than silently mutate.
    """
    values.setflags(write=False)
    return values


def estimate_pool_bytes(pool: ValuePool) -> int:
    """Conservative byte-size estimate for cache budgeting.

    Per S5 spec §4 "Bytes accounting": numpy.nbytes for fixed-width
    dtypes; for object-dtype arrays (strings) the engine uses a fast
    estimate capped at 4 bytes/char to prevent pathological cases.
    """
    if pool.values.dtype != object:
        return int(pool.values.nbytes)
    # Object dtype: estimate via sys.getsizeof per element, capped at
    # len(str)*4 to avoid pathological deep-recursion sizes.
    total = 0
    for v in pool.values:
        if isinstance(v, str):
            total += min(len(v) * 4, 4096)  # cap at 4KB per string
        else:
            total += 64  # rough fallback for non-string objects
    return total


__all__ = ["ValuePool", "_freeze_array", "estimate_pool_bytes"]


def _typing_compat() -> Any:
    """Stub for mypy; not used at runtime."""
    return None

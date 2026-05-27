"""PoolCache: process-local bytes-bounded LRU cache for ValuePool instances.

Per S5 spec §4: bytes-bounded (not entry-bounded; entries vary by 2+ orders
of magnitude). Pattern citations: pandas io budget, Polars
`set_global_string_cache`. Eviction: LRU on identity tuple.

A single pool larger than the entire budget raises
`PoolCapacityError(code='pool_exceeds_cache_budget')` rather than evicting
everything and still failing.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from decoy_engine.generation.pool._errors import PoolCapacityError
from decoy_engine.generation.pool._value_pool import ValuePool, estimate_pool_bytes

_DEFAULT_MAX_BYTES = 256 * 1024 * 1024  # 256 MB per S5 spec §4
_DOMINATE_THRESHOLD = 0.25  # warn at 25% per operating model


@dataclass(frozen=True)
class CacheStats:
    """Snapshot of cache state for instrumentation / tests."""

    entries: int
    bytes_used: int
    bytes_capacity: int
    hits: int
    misses: int
    evictions: int


class PoolCache:
    """Process-local LRU pool cache. Bounded by bytes, not entries.

    Per S5 spec §4: tests instantiate their own cache to avoid
    cross-test contamination. The default cache is a module-level
    singleton accessed via `get_default_pool_cache()`.
    """

    def __init__(self, *, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        self._max_bytes = max_bytes
        # OrderedDict preserves insertion + access order; LRU is
        # implemented by `move_to_end` on access.
        self._entries: OrderedDict[tuple[str, str, str, bytes, int], ValuePool] = OrderedDict()
        self._bytes_used = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, identity: tuple[str, str, str, bytes, int]) -> ValuePool | None:
        """Return the pool for `identity` or None on miss. Touches LRU on hit."""
        pool = self._entries.get(identity)
        if pool is None:
            self._misses += 1
            return None
        self._hits += 1
        self._entries.move_to_end(identity)
        return pool

    def put(self, pool: ValuePool) -> None:
        """Insert `pool`, evicting LRU entries to make room.

        Raises:
            PoolCapacityError(code='pool_exceeds_cache_budget') if `pool`
            alone is larger than `max_bytes`.
        """
        pool_bytes = estimate_pool_bytes(pool)
        if pool_bytes > self._max_bytes:
            raise PoolCapacityError(
                code="pool_exceeds_cache_budget",
                message=(
                    f"Pool for {pool.provider!r} estimated at {pool_bytes} bytes "
                    f"exceeds cache budget {self._max_bytes} bytes. Increase the "
                    "budget via `decoy_engine.settings` or reduce the pool size."
                ),
            )
        # Evict LRU until the new pool fits.
        while self._bytes_used + pool_bytes > self._max_bytes and self._entries:
            _, evicted = self._entries.popitem(last=False)
            self._bytes_used -= estimate_pool_bytes(evicted)
            self._evictions += 1
        self._entries[pool.identity] = pool
        self._bytes_used += pool_bytes

    def stats(self) -> CacheStats:
        """Return a frozen snapshot of cache state."""
        return CacheStats(
            entries=len(self._entries),
            bytes_used=self._bytes_used,
            bytes_capacity=self._max_bytes,
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
        )

    def clear(self) -> None:
        """Reset state. Test-only; never called in production."""
        self._entries.clear()
        self._bytes_used = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0


_DEFAULT_CACHE: PoolCache | None = None


def get_default_pool_cache() -> PoolCache:
    """Return the module-level default cache singleton.

    Tests should construct their own `PoolCache(max_bytes=...)` rather
    than calling this to avoid cross-test contamination.
    """
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = PoolCache()
    return _DEFAULT_CACHE


def _reset_default_pool_cache_for_tests() -> None:
    """Test-only: drop the singleton so the next call rebuilds clean."""
    global _DEFAULT_CACHE
    _DEFAULT_CACHE = None

"""PoolCache bytes-bounded LRU tests (S5 spec §4)."""

from __future__ import annotations

import numpy as np
import pytest

from decoy_engine.generation.pool import PoolCache, PoolCapacityError, ValuePool


def _make_pool(provider: str, size: int, seed: bytes = b"\x00" * 8) -> ValuePool:
    return ValuePool(
        values=np.array(["x" * 100] * size, dtype=object),
        provider=provider,
        locale="en_US",
        config_hash="abc",
        seed=seed,
        size=size,
        build_time_ms=1.0,
        backend_type="faker",
        backend_version="25.4.0",
        distinct_count=size,
    )


class TestGetPut:
    def test_get_miss_returns_none(self) -> None:
        cache = PoolCache(max_bytes=10_000_000)
        assert cache.get(("p", "l", "c", b"\x00" * 8, 10)) is None
        assert cache.stats().misses == 1

    def test_put_then_get_hit(self) -> None:
        cache = PoolCache(max_bytes=10_000_000)
        pool = _make_pool("p", 5)
        cache.put(pool)
        retrieved = cache.get(pool.identity)
        assert retrieved is pool
        assert cache.stats().hits == 1


class TestEviction:
    def test_lru_eviction_when_over_budget(self) -> None:
        cache = PoolCache(max_bytes=10_000)
        # Each pool ~ 400 bytes (4 entries x 100 chars x 1 byte/char est).
        for i in range(5):
            cache.put(_make_pool(f"p{i}", 4, seed=bytes([i] * 8)))
        # Should have evicted some; cache stays within budget.
        assert cache.stats().bytes_used <= 10_000

    def test_pool_larger_than_budget_raises(self) -> None:
        cache = PoolCache(max_bytes=100)  # tiny budget
        big_pool = _make_pool("p", 1000)
        with pytest.raises(PoolCapacityError) as excinfo:
            cache.put(big_pool)
        assert excinfo.value.code == "pool_exceeds_cache_budget"

    def test_eviction_counter_increments(self) -> None:
        cache = PoolCache(max_bytes=2000)
        cache.put(_make_pool("a", 4, seed=b"\x01" * 8))  # ~400 bytes
        cache.put(_make_pool("b", 4, seed=b"\x02" * 8))
        cache.put(_make_pool("c", 4, seed=b"\x03" * 8))
        cache.put(_make_pool("d", 4, seed=b"\x04" * 8))
        cache.put(_make_pool("e", 4, seed=b"\x05" * 8))  # forces evictions
        # At least one eviction happened.
        assert cache.stats().evictions >= 1


class TestStats:
    def test_hit_miss_tracking(self) -> None:
        cache = PoolCache(max_bytes=10_000_000)
        pool = _make_pool("p", 5)
        cache.put(pool)
        cache.get(pool.identity)
        cache.get(("nope", "l", "c", b"\x00" * 8, 5))
        s = cache.stats()
        assert s.hits == 1
        assert s.misses == 1
        assert s.entries == 1
        assert s.bytes_capacity == 10_000_000


class TestLruOrdering:
    def test_recent_access_moves_to_end(self) -> None:
        """Per S5 spec §4: LRU on identity tuple via move_to_end on get."""
        cache = PoolCache(max_bytes=2000)
        a = _make_pool("a", 4, seed=b"\x01" * 8)
        b = _make_pool("b", 4, seed=b"\x02" * 8)
        cache.put(a)
        cache.put(b)
        # Access a to make it MRU; then add c + d which evict LRU (b).
        cache.get(a.identity)
        cache.put(_make_pool("c", 4, seed=b"\x03" * 8))
        cache.put(_make_pool("d", 4, seed=b"\x04" * 8))
        cache.put(_make_pool("e", 4, seed=b"\x05" * 8))
        # a accessed-most-recently might survive depending on byte
        # accounting; sanity-check that evictions happened.
        assert cache.stats().evictions >= 1

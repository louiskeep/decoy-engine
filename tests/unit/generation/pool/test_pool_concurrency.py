"""Sprint P5: faker pool determinism under concurrent workers.

Three acceptance cells from docs/job-performance-sprints.md §3 Sprint P5:

1. Same-identity equality: building the same pool identity in independent
   workers (threads here, a subprocess for true cross-process isolation)
   produces byte-identical values.
2. Different-identity non-interference: concurrent builds for different
   identities match their serial references (the anti-race assertion; a
   shared mutable Faker RNG would interleave draws between builds).
3. Cache-key completeness: two builds differing only in provider config,
   pool size, locale, namespace, or job seed never collide to one cached
   pool.

All builds go through ``get_default_registry()`` on purpose: the singleton
registry shares one FakerAdapter across every pool build, which is exactly
the production topology the P5 hazard lives in.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from decoy_engine.generation.pool import PoolBuilder, PoolCache
from decoy_engine.providers_v2 import get_default_registry

_SEED = b"\x00\x00\x00\x00\x00\x00\x00\x2a"  # 42
_POOL_SIZE = 400  # large enough that interleaved RNG draws cannot go unnoticed


def _build_values(
    *,
    job_seed: bytes = _SEED,
    namespace: str = "p5",
    size: int = _POOL_SIZE,
    locale: str | None = None,
    config: dict[str, Any] | None = None,
) -> list[Any]:
    pool = PoolBuilder(get_default_registry()).build(
        "person_email",
        size=size,
        job_seed=job_seed,
        locale=locale,
        config=config,
        namespace=namespace,
    )
    return list(pool.values)


class TestSameIdentityConcurrency:
    def test_same_identity_concurrent_threads_identical(self) -> None:
        """P5 acceptance: rebuilding the same pool identity in N concurrent
        workers produces values identical to a serial build. A barrier forces
        every worker into generate_batch at once so a shared seeded Faker
        instance would race (seed A, seed B, draw with B's state)."""
        serial = _build_values()
        n_workers = 8
        barrier = threading.Barrier(n_workers)

        def worker() -> list[Any]:
            barrier.wait()
            return _build_values()

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            results = [f.result() for f in [pool.submit(worker) for _ in range(n_workers)]]
        for i, values in enumerate(results):
            assert values == serial, f"worker {i} diverged from the serial build"

    def test_same_identity_subprocess_identical(self) -> None:
        """P5 acceptance (process isolation): an independent worker process
        building the same identity produces the same values. Mirrors the
        mimesis cross-process reproducibility cell."""
        script = (
            "import json;"
            "from decoy_engine.generation.pool import PoolBuilder;"
            "from decoy_engine.providers_v2 import get_default_registry;"
            "pool=PoolBuilder(get_default_registry()).build("
            "'person_email',size=64,job_seed=bytes([0]*7+[42]),namespace='p5');"
            "print(json.dumps([str(v) for v in pool.values]))"
        )
        result = subprocess.run(  # noqa: S603 -- args are test literals, not untrusted input
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )
        child = json.loads(result.stdout.strip())
        parent = [str(v) for v in _build_values(size=64)]
        assert child == parent


class TestDifferentIdentityConcurrency:
    def test_different_identities_concurrent_match_serial(self) -> None:
        """P5 acceptance: concurrent builds for DIFFERENT identities do not
        contaminate each other's RNG. Each identity's concurrent result must
        equal its serial reference; with a shared mutable Faker a competing
        build's seed_instance lands between this build's seed and draws."""
        seeds = [bytes([0] * 7 + [i]) for i in range(8)]
        serial = {seed: _build_values(job_seed=seed) for seed in seeds}
        barrier = threading.Barrier(len(seeds))

        def worker(seed: bytes) -> list[Any]:
            barrier.wait()
            return _build_values(job_seed=seed)

        with ThreadPoolExecutor(max_workers=len(seeds)) as pool:
            futures = {seed: pool.submit(worker, seed) for seed in seeds}
            for seed, future in futures.items():
                assert future.result() == serial[seed], (
                    f"seed {seed.hex()} diverged under concurrency; "
                    "pool builds are sharing mutable RNG state"
                )


class TestCacheKeyCompleteness:
    """P5 acceptance: pool identity is keyed by provider, locale, namespace,
    seed, pool size, AND provider config. Namespace and job seed enter the
    identity tuple through the HKDF-derived pool_seed field; config through
    its canonical hash; locale, provider, and size directly."""

    def _identity(self, **kwargs: Any) -> tuple[str, str, str, bytes, int]:
        defaults: dict[str, Any] = {
            "size": 32,
            "job_seed": _SEED,
            "locale": None,
            "config": None,
            "namespace": "p5",
        }
        defaults.update(kwargs)
        return PoolBuilder(get_default_registry()).identity_for("person_email", **defaults)

    def test_config_only_difference_distinct_identity(self) -> None:
        assert self._identity(config={"domain": "a.com"}) != self._identity(
            config={"domain": "b.com"}
        )

    def test_pool_size_only_difference_distinct_identity(self) -> None:
        assert self._identity(size=32) != self._identity(size=64)

    def test_locale_only_difference_distinct_identity(self) -> None:
        assert self._identity(locale="en_US") != self._identity(locale="de_DE")

    def test_namespace_only_difference_distinct_identity(self) -> None:
        assert self._identity(namespace="ns_a") != self._identity(namespace="ns_b")

    def test_job_seed_only_difference_distinct_identity(self) -> None:
        assert self._identity(job_seed=b"\x00" * 8) != self._identity(job_seed=b"\x01" * 8)

    def test_config_only_difference_not_served_same_cached_pool(self) -> None:
        """Two builds differing only in provider config must not collide to
        one cached pool."""
        builder = PoolBuilder(get_default_registry())
        cache = PoolCache()
        pool_a = builder.build(
            "person_email", size=32, job_seed=_SEED, config={"domain": "a.com"}, namespace="p5"
        )
        pool_b = builder.build(
            "person_email", size=32, job_seed=_SEED, config={"domain": "b.com"}, namespace="p5"
        )
        cache.put(pool_a)
        cache.put(pool_b)
        assert cache.get(pool_a.identity) is pool_a
        assert cache.get(pool_b.identity) is pool_b
        assert cache.stats().entries == 2


class TestPoolCacheThreadSafety:
    def test_concurrent_put_get_keeps_accounting_consistent(self) -> None:
        """P5 hardening: PoolCache is a process-shared structure; concurrent
        put/get from parallel workers must not corrupt LRU order or the
        bytes accounting. Regression guard for the internal cache lock."""
        builder = PoolBuilder(get_default_registry())
        pools = [
            builder.build("person_email", size=16, job_seed=_SEED, namespace=f"ns{i}")
            for i in range(16)
        ]
        cache = PoolCache()
        barrier = threading.Barrier(8)

        def worker(offset: int) -> None:
            barrier.wait()
            for _ in range(50):
                for pool in pools[offset::2]:
                    cache.put(pool)
                    cache.get(pool.identity)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker, i % 2) for i in range(8)]
            for future in futures:
                future.result()

        stats = cache.stats()
        assert stats.entries == len(pools)
        # Exact accounting: re-puts of an already-cached identity must not
        # double-count bytes (each pool is counted exactly once).
        from decoy_engine.generation.pool._value_pool import estimate_pool_bytes

        assert stats.bytes_used == sum(estimate_pool_bytes(p) for p in pools)
        for pool in pools:
            assert cache.get(pool.identity) is pool

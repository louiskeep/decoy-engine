"""Run-scoped faker pool lifecycle for the out-of-core route (B3).

The out-of-core route masks one batch at a time, so a faker column that draws
from a provider pool needs that pool built ONCE and reused across every batch of
the run. The pandas oracle and the native chunked route already do this with a
per-run ``PoolCache`` plus the shared ``resolve_faker_pool_identity`` split (see
``execution/_strategies/_faker.py`` and ``execution/native/_chunk_masking.py``);
this module gives the out-of-core route the same transport, scoped to one run.

This slice does NOT admit faker on the out-of-core route. The route still
declines it with ``out_of_core_faker_pool_unsupported`` at
``out_of_core/_compat.py``. This context is the bounded, run-scoped pool
lifecycle that a later slice (B4) consults when it wires the deterministic REUSE
faker payload onto the route. Built here first, on its own, so its memory
bounding and identity reuse can be proven in isolation.

Established pattern reused, not re-invented: ``PoolBuilder`` builds, ``PoolCache``
stores, ``resolve_faker_pool_identity`` keys. The one thing added over a bare
``PoolCache`` is a pre-build reservation gate: a pool is refused BEFORE the
provider is allocated when it would not fit the run's pool allowance, so a job
can never grow pool memory without a bound and never sizes a pool from a batch
or source row count. The gate's per-value upper bound mirrors ``PoolCache``'s
own 4096-byte-per-string cap in ``estimate_pool_bytes``, so the conservative
pre-build reservation is always at least the pool's measured post-build size and
the run cache never evicts an in-allowance workload (one build per identity
holds across batches).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.generation.pool._builder import PoolBuilder
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._identity import PoolIdentity, resolve_faker_pool_identity
from decoy_engine.generation.pool._value_pool import ValuePool
from decoy_engine.providers_v2 import ProviderRegistry

# Conservative per-value upper bound for the PRE-BUILD reservation. Mirrors the
# 4096-byte-per-string cap PoolCache.estimate_pool_bytes applies post-build, so
# `pool_size * _RESERVE_BYTES_PER_VALUE` is always at least the pool's measured
# size: the run cache therefore never has to evict an in-allowance workload, and
# the "one build per identity across batches" guarantee holds.
#
# This bound holds for every pool the stock PoolBuilder produces, which are
# object-dtype arrays of exactly `size` elements (PoolBuilder.build forces
# dtype=object), so estimate_pool_bytes takes its object branch and each element
# contributes at most 4096 bytes. It would NOT bound a non-standard builder that
# returned more than `size` elements or a fixed-width dtype wider than 4096 bytes
# per item; the route only ever builds through the stock builder, and PoolCache
# itself still fails closed (pool_exceeds_cache_budget) on any single pool larger
# than the whole allowance.
_RESERVE_BYTES_PER_VALUE = 4096


@dataclass(frozen=True)
class PreparedFakerPool:
    """A pinned pool plan for one faker column: everything needed to build or
    look up its pool, resolved once so every batch of the run keys the same way.

    ``identity`` is the ``PoolCache`` key from the shared
    ``resolve_faker_pool_identity`` split, so a pool prepared here lands on the
    exact same cache identity the oracle and native chunked routes use.
    """

    provider: str
    locale: str | None
    build_config: dict[str, Any]
    namespace: str | None
    pool_size: int
    identity: PoolIdentity


class OocFakerPoolContext:
    """Owns one run's faker pools: build-once, reuse-across-batches, bounded.

    One instance per out-of-core run. State (the cache, the reservation ledger,
    the build count) is instance-local, so two runs never share pools and a
    run's pools are released when the context is closed. Pool construction is
    seeded by ``job_seed`` through ``PoolBuilder``; deterministic SELECTION from
    the pool (keyed by the mask key) is a later slice's concern and does not live
    here, matching the build/sample split the oracle already draws.
    """

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        job_seed: bytes,
        max_pool_bytes: int,
        builder: PoolBuilder | None = None,
        reserve_bytes_per_value: int = _RESERVE_BYTES_PER_VALUE,
    ) -> None:
        if max_pool_bytes <= 0:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_POOL_BUDGET_INVALID,
                message=f"max_pool_bytes must be positive, got {max_pool_bytes}.",
            )
        if reserve_bytes_per_value <= 0:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_POOL_BUDGET_INVALID,
                message=f"reserve_bytes_per_value must be positive, got {reserve_bytes_per_value}.",
            )
        self._registry = registry
        self._job_seed = job_seed
        self._max_pool_bytes = max_pool_bytes
        self._reserve_per_value = reserve_bytes_per_value
        # Reuse the builder the identity split expects; default to one bound to
        # this run's registry so a rebound provider can never alias another
        # binding's pool (PoolBuilder keys the adapter serial into the identity).
        self._builder = builder if builder is not None else PoolBuilder(registry)
        # A fresh per-run cache, never the module singleton: a run's pools must
        # not bleed into another run's cache (cross-job key contamination).
        self._cache = PoolCache(max_bytes=max_pool_bytes)
        self._reserved_bytes = 0
        self._builds = 0

    def prepare(
        self,
        *,
        provider: str,
        namespace: str | None,
        cfg: dict[str, Any],
        plan_pool_size: int | None = None,
    ) -> PreparedFakerPool:
        """Resolve the pool plan + cache identity for one faker column.

        Cheap: no pool is built here (``resolve_faker_pool_identity`` uses
        ``PoolBuilder.identity_for``, not ``build``). A non-positive resolved
        pool size is refused up front with a caller-visible code.
        """
        pool_size, locale, build_config, identity = resolve_faker_pool_identity(
            builder=self._builder,
            provider=provider,
            plan_pool_size=plan_pool_size,
            namespace=namespace,
            job_seed=self._job_seed,
            cfg=cfg,
        )
        if pool_size <= 0:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_POOL_SIZE_INVALID,
                message=(
                    f"faker pool for provider {provider!r} resolved to a non-positive "
                    f"pool_size {pool_size}; a bounded positive pool size is required."
                ),
            )
        return PreparedFakerPool(
            provider=provider,
            locale=locale,
            build_config=build_config,
            namespace=namespace,
            pool_size=pool_size,
            identity=identity,
        )

    def get_pool(self, prepared: PreparedFakerPool) -> ValuePool:
        """Return the pool for ``prepared``, building it at most once per run.

        On a cache hit the stored pool is returned with no rebuild. On a miss the
        pool is reserved against the run allowance BEFORE the provider is
        allocated: an over-allowance plan raises
        ``out_of_core_faker_pool_budget_exceeded`` and no pool is built. The
        reservation is sized from ``pool_size`` alone, never a batch or source
        row count.

        A provider-primitive failure (e.g. ``PoolCapacityError`` with
        ``provider_not_poolable`` for a provider the builder cannot pool)
        propagates unwrapped rather than being reshaped into an
        ``out_of_core_faker_pool_*`` code: it is a real provider error, surfaced
        not swallowed. The route admission that reaches this context restricts
        the provider to the poolable faker allowlist, so that path is not taken
        in practice.
        """
        cached = self._cache.get(prepared.identity)
        if cached is not None:
            return cached
        reservation = prepared.pool_size * self._reserve_per_value
        if self._reserved_bytes + reservation > self._max_pool_bytes:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_POOL_BUDGET_EXCEEDED,
                message=(
                    f"faker pool for provider {prepared.provider!r} "
                    f"(pool_size {prepared.pool_size}) needs {reservation} reserved bytes; "
                    f"{self._reserved_bytes} of {self._max_pool_bytes} already reserved for "
                    "this run. Reduce pool_size or raise the out-of-core pool budget."
                ),
            )
        pool = self._builder.build(
            prepared.provider,
            size=prepared.pool_size,
            job_seed=self._job_seed,
            locale=prepared.locale,
            config=prepared.build_config,
            namespace=prepared.namespace,
        )
        self._cache.put(pool)
        self._reserved_bytes += reservation
        self._builds += 1
        return pool

    @property
    def builds(self) -> int:
        """Pools built this run (cache misses that led to a real build)."""
        return self._builds

    @property
    def reserved_bytes(self) -> int:
        """Bytes reserved against the run allowance so far (conservative)."""
        return self._reserved_bytes

    @property
    def pool_count(self) -> int:
        """Distinct pools currently held for this run."""
        return self._cache.stats().entries

    def close(self) -> None:
        """Release this run's pools and reset all run-scoped state."""
        self._cache.clear()
        self._reserved_bytes = 0
        self._builds = 0

    def __enter__(self) -> OocFakerPoolContext:
        return self

    def __exit__(self, *_exc: object) -> None:
        # Returns None (never True), so an exception in the run is never
        # swallowed by the pool teardown.
        self.close()


__all__ = ["OocFakerPoolContext", "PreparedFakerPool"]

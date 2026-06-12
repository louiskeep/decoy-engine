"""PoolAdapter: the first BackendAdapter that boosts supports_deterministic.

Per S5 spec §10 + PO PQ1 call: PoolAdapter wraps another BackendAdapter
(e.g. FakerAdapter) and routes deterministic calls through a built pool
indexed by `derive_index`. The capability_matrix view flips
`supports_deterministic=True` for poolable providers; the live registry
view stays False (the registry is the source of truth WHEN routed
directly through the wrapped adapter; PoolAdapter's view is the source
of truth WHEN routed through the wrapping pattern). S9 picks the right
adapter per column at execute time; S5 ships the wrapping primitive.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

from decoy_engine.determinism import derive_index
from decoy_engine.generation.pool._builder import PoolBuilder
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.providers_v2 import (
    BackendAdapter,
    CapabilityMatrix,
    ProviderError,
    ProviderSpec,
)


class PoolAdapter:
    """BackendAdapter that wraps another adapter and routes deterministic
    requests through a pool indexed by derive_index.

    Implements `decoy_engine.providers_v2.BackendAdapter` Protocol (PEP 544).
    """

    backend_type: str = "pool"

    def __init__(
        self,
        wrapped: BackendAdapter,
        *,
        builder: PoolBuilder,
        cache: PoolCache,
    ) -> None:
        self._wrapped = wrapped
        self._builder = builder
        self._cache = cache
        # Per-identity build locks: concurrent misses on the SAME
        # identity must not each run builder.build() (divergent pool
        # instances break the determinism contract under concurrency);
        # builds for different identities stay parallel. The dict is
        # bounded by the distinct identities seen by this adapter
        # instance (per-job in practice).
        self._build_locks: dict[Any, threading.Lock] = {}
        self._build_locks_guard = threading.Lock()
        # backend_version is a per-instance attribute: "pool(<wrapped>)".
        self.backend_version: str = f"pool({wrapped.backend_version})"

    def _build_or_get_pool(
        self,
        provider: str,
        spec: ProviderSpec,
    ) -> Any:
        """Cache-checked pool fetch: identity first, build only on a miss.

        Per S5 spec §3.1: pool seed derives from job seed + namespace +
        config hash. Cache key is the ValuePool.identity tuple.

        S5 F1: derive the identity via `PoolBuilder.identity_for(...)` and
        consult the cache BEFORE building. The prior code built first and
        discarded the result on a cache hit, so every deterministic generate
        paid a full pool rebuild and the cache-hit performance gate was
        structurally unreachable.
        """
        if spec.seed is None:
            raise GenerationError(
                code="deterministic_requires_source_and_namespace",
                message=(
                    "PoolAdapter deterministic path requires ProviderSpec.seed "
                    "to be set (the job seed bytes); got None."
                ),
            )
        # NF4: `pool_size` is a capacity knob carried in spec.extra. It drives
        # the pool size and is kept out of the value-generation config, which
        # the builder both hashes into the identity and forwards as Faker
        # kwargs (a stray `pool_size=` kwarg would break the Faker call). The
        # prior code hardcoded size=10_000, so the config knob was dead.
        build_config = {k: v for k, v in spec.extra.items() if k != "pool_size"}
        size = int(spec.extra.get("pool_size", 10_000))
        identity = self._builder.identity_for(
            provider,
            size=size,
            job_seed=spec.seed,
            locale=spec.locale,
            config=build_config,
            namespace=spec.namespace,
        )
        cached = self._cache.get(identity)
        if cached is not None:
            return cached
        # Double-checked per-identity locking: re-consult the cache
        # under the lock so the losers of the race reuse the winner's
        # pool instead of rebuilding (and possibly caching a divergent
        # instance).
        with self._build_locks_guard:
            lock = self._build_locks.setdefault(identity, threading.Lock())
        with lock:
            cached = self._cache.get(identity)
            if cached is not None:
                return cached
            pool = self._builder.build(
                provider=provider,
                size=size,
                job_seed=spec.seed,
                locale=spec.locale,
                config=build_config,
                namespace=spec.namespace,
            )
            self._cache.put(pool)
            return pool

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | None = None,
    ) -> Any:
        """Deterministic: build/fetch pool + index via derive_index.
        Non-deterministic: delegate to the wrapped adapter."""
        if spec.deterministic:
            if source_value is None:
                raise ProviderError(
                    code="deterministic_requires_source_value",
                    message=(
                        "PoolAdapter.generate(deterministic=True) requires "
                        "source_value to key derive_index; got None."
                    ),
                )
            pool = self._build_or_get_pool(provider, spec)
            # spec.seed + spec.namespace validated by ProviderSpec.__post_init__
            # when deterministic=True (raises ProviderError otherwise); both
            # are non-None here. Cast for mypy:
            if spec.seed is None or spec.namespace is None:
                raise ProviderError(
                    code="deterministic_requires_namespace_and_seed",
                    message="seed/namespace must be set; defensive guard.",
                )
            idx = derive_index(
                seed=spec.seed,
                namespace=spec.namespace,
                source=source_value,
                pool_size=pool.size,
            )
            return pool.values[idx]
        # Non-deterministic: bypass pool, route through wrapped adapter.
        return self._wrapped.generate(provider, spec=spec, source_value=None)

    def generate_batch(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        count: int,
    ) -> Sequence[Any]:
        """Always delegates to wrapped (batch is non-deterministic by design;
        per S4 spec §2: 'generate_batch has no per-row source value to key on')."""
        return self._wrapped.generate_batch(provider, spec=spec, count=count)

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        """Return wrapped capability matrix with `supports_deterministic`
        boosted to True for poolable providers + `backend_type` set to "pool".

        Per S5 spec §10: model_copy preserves all other fields. Non-poolable
        providers pass through unchanged (no deterministic boost).
        """
        wrapped_caps = self._wrapped.capability_matrix(provider)
        if not wrapped_caps.poolable:
            return wrapped_caps
        return wrapped_caps.model_copy(
            update={
                "supports_deterministic": True,
                "backend_type": "pool",
                "backend_version": self.backend_version,
            }
        )

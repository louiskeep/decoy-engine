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
        # backend_version is a per-instance attribute: "pool(<wrapped>)".
        self.backend_version: str = f"pool({wrapped.backend_version})"

    def _build_or_get_pool(
        self,
        provider: str,
        spec: ProviderSpec,
        size: int = 10_000,
    ) -> Any:
        """Cache-checked pool fetch.

        Per S5 spec §3.1: pool seed derives from job seed + namespace +
        config hash. Cache key is the ValuePool.identity tuple.
        """
        if spec.seed is None:
            raise GenerationError(
                code="deterministic_requires_source_and_namespace",
                message=(
                    "PoolAdapter deterministic path requires ProviderSpec.seed "
                    "to be set (the job seed bytes); got None."
                ),
            )
        # We need to derive the cache identity. The builder does this
        # internally during build; for cache lookup we replicate the
        # derivation here. To avoid duplication, we just build (which
        # is cheap on cache hit because we check the cache after deriving
        # identity).
        # Strategy: build first to a temp ValuePool, then look up; if hit,
        # discard the build. This is a stub-grade approach for S5; S9
        # routing can prefetch + check identity before building.
        # Actually: the builder is deterministic given the same inputs;
        # so the safer pattern is to call the builder once and let the
        # cache short-circuit via put-after-get.
        pool = self._builder.build(
            provider=provider,
            size=size,
            job_seed=spec.seed,
            locale=spec.locale,
            config=spec.extra,
            namespace=spec.namespace,
        )
        cached = self._cache.get(pool.identity)
        if cached is not None:
            return cached
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

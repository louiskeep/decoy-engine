"""PoolBuilder: one generate_batch call per pool.

Per S5 spec §3: PoolBuilder asks the registry for the adapter, validates
`CapabilityMatrix.poolable: True`, constructs a non-deterministic
`ProviderSpec` (deterministic=False always at build time per PO call),
and calls `adapter.generate_batch(provider, spec=spec, count=size)`.
Single call. Result becomes `pool.values` after dtype normalization.

**Determinism at build time:** `deterministic=False`. Per S5 spec §3
and the S5 PO call: determinism for the column happens at SAMPLE time
via `derive_index(...)`, not at BUILD time. A deterministic pool is
just a non-deterministic pool that the sampler indexes deterministically.

**Pool seed derivation** (S5 spec §3.1): the pool's `seed` field is NOT
the job seed. It's a per-pool derived seed from
`derive(job_seed, namespace="pool/{provider}/{locale}/{namespace}", source=config_hash)`
so two columns using the same provider + locale + config but in
different namespaces have different pools.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import weakref
from typing import Any

import numpy as np

from decoy_engine.determinism import derive
from decoy_engine.generation.pool._errors import PoolCapacityError
from decoy_engine.generation.pool._value_pool import ValuePool, _freeze_array
from decoy_engine.providers_v2 import ProviderRegistry, ProviderSpec


def _config_hash(config: dict[str, Any] | None) -> str:
    """SHA-256 over canonical JSON of the ProviderSpec.extra dict."""
    canonical = json.dumps(
        config or {}, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


_ADAPTER_SERIAL: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
_ADAPTER_SERIAL_PINNED: dict[int, tuple[Any, int]] = {}
_next_serial = 0
_SERIAL_LOCK = threading.Lock()


def _adapter_serial(adapter: Any) -> int:
    """A process-unique number for this adapter object, never reused.

    `id()` alone can be recycled after the adapter is collected, which would
    alias a rebound provider onto an older cached pool. A weak map keeps the
    number tied to the live object; an adapter that cannot be weakly referenced
    is pinned instead (rare, and a registry holds its adapters for its life).
    """
    global _next_serial
    with _SERIAL_LOCK:
        try:
            serial = _ADAPTER_SERIAL.get(adapter)
            if serial is None:
                _next_serial += 1
                serial = _next_serial
                _ADAPTER_SERIAL[adapter] = serial
            return serial
        except TypeError:
            pinned = _ADAPTER_SERIAL_PINNED.get(id(adapter))
            if pinned is None or pinned[0] is not adapter:
                _next_serial += 1
                pinned = (adapter, _next_serial)
                _ADAPTER_SERIAL_PINNED[id(adapter)] = pinned
            return pinned[1]


def _binding_token(registry: ProviderRegistry, provider: str) -> str:
    """Names the adapter the registry resolves for `provider`.

    The pool cache is process-local, so the adapter object's serial is a safe
    part of the key: a registry that rebinds the provider (a different adapter
    class, or a different instance) never reads a pool another binding built.
    An unresolvable provider gets a fixed token; `build` raises for it anyway.
    """
    try:
        adapter = registry.get_adapter(provider)
    except Exception:  # unknown provider: nothing was bound, nothing to alias
        return "unbound"
    cls = type(adapter)
    return f"{cls.__module__}.{cls.__qualname__}#{_adapter_serial(adapter)}"


def _derive_pool_seed(
    job_seed: bytes,
    provider: str,
    locale: str,
    namespace: str | None,
    config_hash: str,
) -> bytes:
    """Per S5 spec §3.1: pool_seed = first 8 bytes of derive(...).

    The `pool/{provider}/{locale}/{namespace}` namespace shape is canonical
    per S5 spec §3.1; any change is a SEED_PROTOCOL_VERSION bump.
    """
    pool_namespace = f"pool/{provider}/{locale or 'default'}/{namespace or '_default'}"
    return derive(
        seed=job_seed,
        namespace=pool_namespace,
        source=config_hash.encode("utf-8"),
    )[:8]


class PoolBuilder:
    """Build a ValuePool by delegating to the registry-resolved adapter.

    One `generate_batch` call per `build(...)` invocation. The builder
    does NOT call `derive(...)` itself for per-row determinism; that's
    the sampler's job. The builder uses the pool_seed only to seed the
    underlying RNG (via the adapter) for build-time reproducibility.
    """

    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry

    def identity_for(
        self,
        provider: str,
        *,
        size: int,
        job_seed: bytes,
        locale: str | None = None,
        config: dict[str, Any] | None = None,
        namespace: str | None = None,
    ) -> tuple[str, str, str, bytes, int]:
        """Compute the cache identity for a pool WITHOUT building it.

        Byte-identical to `build(...).identity` for the same inputs, but
        cheap (no generate_batch call). PoolAdapter calls this to consult
        the cache before paying for a build (S5 F1): the prior code built
        first and discarded the result on a cache hit, so every
        deterministic generate paid a full rebuild and the >90% cache-hit
        performance gate was structurally unreachable. The derivation reuses
        the same `_config_hash` + `_derive_pool_seed` helpers `build` uses,
        and the field order matches `ValuePool.identity`, so the two cannot
        drift.
        """
        effective_locale = locale or "default"
        cfg_hash = _config_hash(config)
        pool_seed = _derive_pool_seed(job_seed, provider, effective_locale, namespace, cfg_hash)
        # The binding token rides in the identity's config field only. The pool
        # seed above stays derived from the bare config hash, so pool values do
        # not depend on which adapter object served them.
        keyed_hash = f"{cfg_hash}|{_binding_token(self._registry, provider)}"
        return (provider, effective_locale, keyed_hash, pool_seed, size)

    def build(
        self,
        provider: str,
        *,
        size: int,
        job_seed: bytes,
        locale: str | None = None,
        config: dict[str, Any] | None = None,
        namespace: str | None = None,
    ) -> ValuePool:
        """Build a pool of `size` values for `provider`.

        Args:
            provider: semantic name resolved against the registry.
            size: pool capacity (number of values to generate).
            job_seed: 8-byte job seed (plan.seed_envelope.job_seed).
            locale: Faker locale; defaults to adapter's locale.
            config: ProviderSpec.extra dict; canonicalized + hashed.
            namespace: optional; participates in pool-seed derivation
                but NOT in identity tuple.

        Raises:
            PoolCapacityError(code='provider_not_poolable') when the
                provider's CapabilityMatrix declares `poolable: False`.
        """
        caps = self._registry.get_capabilities(provider)
        if not caps.poolable:
            raise PoolCapacityError(
                code="provider_not_poolable",
                message=(
                    f"Provider {provider!r} declares `poolable: False`; "
                    "PoolBuilder cannot build a pool for it. Route through "
                    "the underlying adapter directly via generate(...) per row."
                ),
            )
        adapter = self._registry.get_adapter(provider)
        effective_locale = locale or "default"
        cfg_hash = _config_hash(config)
        pool_seed = _derive_pool_seed(job_seed, provider, effective_locale, namespace, cfg_hash)

        # Build-time ProviderSpec is non-deterministic in the cardinality
        # sense (determinism for the column enters at sample-time via
        # derive_index), but it now carries `seed=pool_seed` so the adapter
        # can seed its RNG for build-time reproducibility (S5 F2). Without
        # this the pool_seed only entered the cache identity while the
        # underlying Faker batch stayed unseeded: two builds of the same
        # identity produced different values, making the identity a lie.
        # `deterministic=False` keeps the S4 contract (seeding is opt-in via
        # a non-None seed, not the cardinality-determinism axis).
        spec = ProviderSpec(
            locale=locale,
            deterministic=False,
            namespace=None,
            seed=pool_seed,
            extra=dict(config or {}),
        )

        start = time.monotonic()
        raw_values = adapter.generate_batch(provider, spec=spec, count=size)
        build_time_ms = (time.monotonic() - start) * 1000.0

        values = np.array(list(raw_values), dtype=object)
        values = _freeze_array(values)

        # Distinct count: use set for object dtype, np.unique for numeric.
        if values.dtype == object:
            distinct_count = len(set(values.tolist()))
        else:
            distinct_count = int(np.unique(values).size)

        return ValuePool(
            values=values,
            provider=provider,
            locale=effective_locale,
            config_hash=f"{cfg_hash}|{_binding_token(self._registry, provider)}",
            seed=pool_seed,
            size=size,
            build_time_ms=build_time_ms,
            backend_type=caps.backend_type,
            backend_version=caps.backend_version,
            distinct_count=distinct_count,
        )

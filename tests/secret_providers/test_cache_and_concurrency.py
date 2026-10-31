"""F13 §5 acceptance 8 and the thread-safety invariant: bounded, deterministic cache lifetime.

Entries are scoped to the provider instance, the admitting registry and the provider's security
context, so two providers sharing a scheme and reference never share a prepared key.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from decoy_engine.keyprovider import MaskSecretError, SecretKeyProvider
from decoy_engine.secret_providers import (
    EXPIRY_SKEW_SECONDS,
    MASK_ROOT_V1,
    REQUIREMENTS,
    ResolveContext,
    SecretCache,
    SecretCachePolicy,
    SecretExpired,
    SecretProviderRegistry,
    SecretProviderUnavailable,
    _cache,
    admit_mask_key,
    default_cache,
    parse_ref,
    prepare_secret,
)
from tests.secret_providers._helpers import FakeClock, ProtocolProvider, keyed_config

REQ = (REQUIREMENTS[MASK_ROOT_V1],)
REF = parse_ref("test:thing?encoding=raw")


def admit(provider, policy=None):
    return prepare_secret(provider=provider, ref=REF, requirements=REQ, cache_policy=policy)


@pytest.fixture(autouse=True)
def _clean_cache():
    default_cache().clear()
    yield
    default_cache().clear()


def test_ttl_zero_never_reuses_across_runs():
    prov = ProtocolProvider()
    for _ in range(3):
        admit(prov, SecretCachePolicy(ttl_seconds=0)).close()
    assert prov.resolve_calls == 3
    assert len(default_cache()) == 0


def test_ttl_over_sixty_is_rejected():
    for bad in (61, -1, True, 1.5):
        with pytest.raises(MaskSecretError):
            SecretCachePolicy(ttl_seconds=bad)  # type: ignore[arg-type]


def test_opt_in_reuse_stops_at_the_exact_deadline():
    clock, prov = FakeClock(), ProtocolProvider()
    policy = SecretCachePolicy(ttl_seconds=30, clock=clock)
    admit(prov, policy).close()
    clock.advance(29.999)
    admit(prov, policy).close()
    assert prov.resolve_calls == 1
    clock.advance(0.001)  # exactly at the deadline: expired
    admit(prov, policy).close()
    assert prov.resolve_calls == 2


def test_hit_does_not_extend_the_ttl():
    clock, prov = FakeClock(), ProtocolProvider()
    policy = SecretCachePolicy(ttl_seconds=10, clock=clock)
    admit(prov, policy).close()
    clock.advance(6)
    admit(prov, policy).close()  # hit at t=6
    clock.advance(5)  # t=11: past the original deadline despite the hit
    admit(prov, policy).close()
    assert prov.resolve_calls == 2


def test_provider_expiry_minus_skew_shortens_the_ttl():
    clock = FakeClock()
    prov = ProtocolProvider(expires_at=clock.wall() + 20)
    policy = SecretCachePolicy(ttl_seconds=60, clock=clock)
    admit(prov, policy).close()
    clock.advance(20 - EXPIRY_SKEW_SECONDS - 0.001)
    admit(prov, policy).close()
    assert prov.resolve_calls == 1
    clock.advance(0.002)
    admit(prov, policy).close()
    assert prov.resolve_calls == 2


def test_an_already_expired_provider_value_fails_closed_and_is_not_cached():
    clock = FakeClock()
    prov = ProtocolProvider(expires_at=1.0)  # long in the past on the wall clock
    with pytest.raises(SecretExpired):
        admit(prov, SecretCachePolicy(ttl_seconds=30, clock=clock))
    assert len(default_cache()) == 0


def test_returned_secrets_are_independent_and_closing_one_wipes_only_it():
    clock, prov = FakeClock(), ProtocolProvider()
    policy = SecretCachePolicy(ttl_seconds=30, clock=clock)
    first = admit(prov, policy)
    key = first.mask_key()
    first.close()
    second = admit(prov, policy)
    assert second.mask_key() == key and not second.closed
    second.close()


def test_errors_evict_and_never_serve_stale_material():
    clock = FakeClock()
    prov = ProtocolProvider()
    policy = SecretCachePolicy(ttl_seconds=30, clock=clock)
    admit(prov, policy).close()
    default_cache().clear()
    prov.fail = SecretExpired
    with pytest.raises(SecretExpired):
        admit(prov, policy)
    assert len(default_cache()) == 0


def test_concurrent_preflight_of_the_same_uncached_secret_reads_once():
    prov = ProtocolProvider()
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    barrier = threading.Barrier(8)
    keys, errors = [], []

    def worker():
        try:
            barrier.wait()
            p = admit(prov, policy)
            keys.append(p.mask_key())
            p.close()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and prov.resolve_calls == 1
    assert len(set(keys)) == 1 and len(keys) == 8


def _admit_ref(provider, ref, policy):
    return prepare_secret(provider=provider, ref=ref, requirements=REQ, cache_policy=policy)


def test_providers_sharing_a_scheme_and_reference_never_share_an_entry():
    # Tenant B's job must never receive tenant A's prepared key from the process cache.
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    tenant_a = ProtocolProvider(value=b"A" * 33)
    tenant_b = ProtocolProvider(value=b"B" * 33)
    key_a, key_b = admit(tenant_a, policy), admit(tenant_b, policy)
    assert tenant_a.resolve_calls == 1 and tenant_b.resolve_calls == 1
    assert key_a.mask_key() == SecretKeyProvider(b"A" * 33).mask_key()
    assert key_b.mask_key() == SecretKeyProvider(b"B" * 33).mask_key()
    for prov, secret in ((tenant_a, b"A" * 33), (tenant_b, b"B" * 33)):
        again = admit(prov, policy)
        assert again.mask_key() == SecretKeyProvider(secret).mask_key()
        again.close()
    assert tenant_a.resolve_calls == 1 and tenant_b.resolve_calls == 1  # each hit its own entry


def test_the_same_provider_in_two_registries_is_cached_per_registry(tmp_path):
    prov = ProtocolProvider()
    cfg = keyed_config(tmp_path, mask_secret_ref="test:thing?encoding=raw")
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    for registry in (SecretProviderRegistry((prov,)), SecretProviderRegistry((prov,))):
        admit_mask_key(cfg, registry=registry, cache_policy=policy).close()
    assert prov.resolve_calls == 2


@dataclass(frozen=True, repr=False)
class _TenantContext(ResolveContext):
    namespace: str = ""


class _NamespacedProvider(ProtocolProvider):
    """Carries a security context (like a Vault namespace) that selects a different secret."""

    def __init__(self) -> None:
        super().__init__()
        self.namespace = "tenant-a"

    def resolve_context(self) -> _TenantContext:
        return _TenantContext(scheme=self.scheme, namespace=self.namespace)

    def resolve(self, ref, context=None):  # type: ignore[override]
        self.value = context.namespace.encode() * 4
        return super().resolve(ref, context)


def test_the_provider_security_context_is_part_of_the_key():
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    prov = _NamespacedProvider()
    first = admit(prov, policy)
    prov.namespace = "tenant-b"
    second = admit(prov, policy)
    assert prov.resolve_calls == 2
    assert first.mask_key() != second.mask_key()
    assert second.mask_key() == SecretKeyProvider(b"tenant-b" * 4).mask_key()


def test_a_failing_security_context_fails_closed():
    class Broken(ProtocolProvider):
        def resolve_context(self):
            raise RuntimeError("F13-CONTEXT-MARKER")

    prov = Broken()
    with pytest.raises(SecretProviderUnavailable) as info:
        admit(prov, SecretCachePolicy(ttl_seconds=30, clock=FakeClock()))
    assert "F13-CONTEXT-MARKER" not in str(info.value) + repr(info.value)
    assert prov.resolve_calls == 0


def test_the_cache_is_bounded_by_capacity():
    capacity = getattr(_cache, "MAX_CACHE_ENTRIES", 128)
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    prov = ProtocolProvider()
    for i in range(capacity + 1):
        _admit_ref(prov, parse_ref(f"test:rotating-{i}?encoding=raw"), policy).close()
    cache = default_cache()
    assert len(cache) == capacity
    assert len(cache._locks) == 0  # single-flight locks leave with their callers


def test_expired_entries_and_their_locks_are_pruned_on_access():
    clock = FakeClock()
    policy = SecretCachePolicy(ttl_seconds=30, clock=clock)
    prov = ProtocolProvider()
    for i in range(5):
        _admit_ref(prov, parse_ref(f"test:unique-{i}?encoding=raw"), policy).close()
    cache = default_cache()
    assert len(cache) == 5  # positive control: they were cached
    clock.advance(30)
    _admit_ref(prov, parse_ref("test:another?encoding=raw"), policy).close()
    assert len(cache) == 1
    assert len(cache._locks) == 0


def test_capacity_eviction_closes_the_evicted_master():
    cache = SecretCache(capacity=2)
    clock = FakeClock()
    masters = []

    def loader():
        return admit(ProtocolProvider())

    owner = object()
    for key in ("a", "b", "c"):
        cache.get_or_load(key, owners=(owner,), ttl=30, clock=clock, loader=loader).close()
        masters.append(list(cache._entries.values())[-1].master)
    assert len(cache) == 2
    assert masters[0].closed and not masters[1].closed and not masters[2].closed
    with pytest.raises(ValueError):
        SecretCache(capacity=0)

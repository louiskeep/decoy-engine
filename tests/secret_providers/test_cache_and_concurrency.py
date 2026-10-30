"""F13 §5 acceptance 8 and the thread-safety invariant: bounded, deterministic cache lifetime."""

from __future__ import annotations

import threading

import pytest

from decoy_engine.keyprovider import MaskSecretError
from decoy_engine.secret_providers import (
    EXPIRY_SKEW_SECONDS,
    MASK_ROOT_V1,
    REQUIREMENTS,
    SecretCachePolicy,
    SecretExpired,
    default_cache,
    parse_ref,
    prepare_secret,
)
from tests.secret_providers._helpers import FakeClock, ProtocolProvider

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

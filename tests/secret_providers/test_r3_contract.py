"""F13 r3: the two recurring classes hold across a race and across a traceback.

Cache scope is bound to ONE security-context snapshot per load (verified again after the read);
invalidation fences in-flight loaders by epoch; a hit honors the calling policy's TTL; the audit
count in the migration evidence is pinned so reverting the document turns a test red.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.secret_providers import (
    MASK_ROOT_V1,
    REQUIREMENTS,
    PreparedSecret,
    SecretBuffer,
    SecretCache,
    SecretCachePolicy,
    SecretProviderUnavailable,
    default_cache,
    parse_ref,
    prepare_secret,
)
from tests.secret_providers._helpers import FakeClock, ProtocolProvider

REQ = (REQUIREMENTS[MASK_ROOT_V1],)
REF = parse_ref("test:thing?encoding=raw")


@pytest.fixture(autouse=True)
def _clean_cache():
    default_cache().clear()
    yield
    default_cache().clear()


def admit(provider, policy):
    return prepare_secret(provider=provider, ref=REF, requirements=REQ, cache_policy=policy)


# --- BLOCKER: the cache scope is one snapshot, bound to the load --------------------------------


class _MutatingProvider(ProtocolProvider):
    """A shared mutable client: its tenant changes at ``flip_at`` while a load is in flight."""

    def __init__(self, flip_at: str) -> None:
        super().__init__()
        self.namespace = "tenant-a"
        self.flip_at = flip_at
        self.flipped = False

    def cache_context(self) -> tuple[str, ...]:
        return (self.namespace,)

    def _flip(self, where: str) -> None:
        if where == self.flip_at and not self.flipped:
            self.flipped = True
            self.namespace = "tenant-b"

    def describe(self, ref):
        self._flip("describe")
        return super().describe(ref)

    def resolve(self, ref):
        self._flip("resolve-before")
        self.value = self.namespace.encode() * 5
        resolved = super().resolve(ref)
        self._flip("resolve-after")
        return resolved


@pytest.mark.parametrize("flip_at", ["describe", "resolve-before", "resolve-after"])
def test_a_client_mutated_mid_load_never_poisons_the_cache(flip_at):
    prov = _MutatingProvider(flip_at)
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    with pytest.raises(SecretProviderUnavailable):
        admit(prov, policy)
    assert len(default_cache()) == 0  # nothing cached under either tenant's key

    served_b = admit(prov, policy)  # the client is now tenant B and stays stable
    assert served_b.mask_key() == SecretKeyProvider(b"tenant-b" * 5).mask_key()
    served_b.close()
    prov.namespace = "tenant-a"
    served_a = admit(prov, policy)
    assert served_a.mask_key() == SecretKeyProvider(b"tenant-a" * 5).mask_key()
    served_a.close()


def test_a_mid_load_mutation_of_a_vault_client_never_poisons_the_cache():
    pytest.importorskip("hvac")
    from decoy_engine.secret_providers.vault import VaultKV2Provider
    from tests.secret_providers.test_vault_adapter import REF as VREF
    from tests.secret_providers.test_vault_adapter import StubClient, payload

    client: Any = StubClient(payload())
    client.url, client.token = "https://vault.test:8200", "tok-a"
    read = client.secrets.kv.v2.read_secret_version

    def racing_read(**kwargs):
        result = read(**kwargs)
        client.token = "tok-b"  # another thread re-authenticates the shared client mid-read
        return result

    client.secrets.kv.v2.read_secret_version = racing_read
    provider = VaultKV2Provider(client)
    policy = SecretCachePolicy(ttl_seconds=30, clock=FakeClock())
    with pytest.raises(SecretProviderUnavailable):
        provider.preflight(VREF, REQ, policy)
    assert len(default_cache()) == 0


# --- HIGH: invalidation fences in-flight loaders ------------------------------------------------


def _prepared() -> PreparedSecret:
    return PreparedSecret(
        SecretBuffer.adopt(bytearray(b"k" * 32)), key_version="v1", requirement_ids=(MASK_ROOT_V1,)
    )


def _blocked_load(cache: SecretCache, owners, interrupt):
    """Start a loader that blocks mid-read, run ``interrupt``, then release it."""
    started, release = threading.Event(), threading.Event()
    loads: list[int] = []

    def loader() -> PreparedSecret:
        loads.append(1)
        started.set()
        assert release.wait(5)
        return _prepared()

    def run() -> None:
        cache.get_or_load("k", owners=owners, ttl=30, clock=FakeClock(), loader=loader).close()

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(5)
    interrupt()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    return loads


def test_invalidate_during_an_inflight_read_inserts_nothing():
    cache, owners = SecretCache(), (object(),)
    _blocked_load(cache, owners, lambda: cache.invalidate("k", owners=owners))
    assert len(cache) == 0


def test_clear_during_an_inflight_read_inserts_nothing():
    cache, owners = SecretCache(), (object(),)
    _blocked_load(cache, owners, cache.clear)
    assert len(cache) == 0


def test_invalidating_another_key_does_not_fence_this_load():
    cache, owners = SecretCache(), (object(),)
    _blocked_load(cache, owners, lambda: cache.invalidate("other", owners=owners))
    assert len(cache) == 1


def test_a_load_started_after_the_invalidation_is_cached():
    cache, owners = SecretCache(), (object(),)
    cache.invalidate("k", owners=owners)
    cache.clear()
    _blocked_load(cache, owners, lambda: None)
    assert len(cache) == 1


# --- MEDIUM: a hit honors the calling policy ----------------------------------------------------


def test_a_hit_is_not_served_past_the_calling_policys_shorter_ttl():
    clock, prov = FakeClock(), ProtocolProvider()
    admit(prov, SecretCachePolicy(ttl_seconds=60, clock=clock)).close()
    clock.advance(2)
    admit(prov, SecretCachePolicy(ttl_seconds=1, clock=clock)).close()
    assert prov.resolve_calls == 2  # the 1 s caller never receives a 2 s old entry

    admit(prov, SecretCachePolicy(ttl_seconds=60, clock=clock)).close()
    admit(prov, SecretCachePolicy(ttl_seconds=60, clock=clock)).close()
    assert prov.resolve_calls == 2  # a caller with a long enough TTL still hits


# --- LOW: the audit count is pinned -------------------------------------------------------------

AUDIT = Path(__file__).resolve().parents[2] / (
    "docs/plans/evidence/2026-10-30-f13-keyed-test-migration-audit.md"
)


@pytest.mark.skipif(
    "MUTANT_UNDER_TEST" in os.environ, reason="mutmut's working copy does not include docs/"
)
def test_the_keyed_test_migration_audit_counts_are_pinned():
    text = AUDIT.read_text(encoding="utf-8")
    section = re.search(r"## Coverage accounting.*?(?=\n## |\Z)", text, re.S)
    assert section is not None
    body = section.group(0)
    for pinned in ("**33,330**", "**33,409**", "**+79**"):
        assert pinned in body
    assert "+78" not in body.replace("+78 (33,408)", "")

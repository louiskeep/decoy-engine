"""Decision 3 (A-4 x F13): local providers (`env:`, `file:`) are never cached.

With a cache TTL above zero, F13's process cache keyed a `file:` secret by reference and provider
only, so a secret admitted under one file-access policy could be served to a run under another
policy that would deny the read. The root-cause fix is that the resolver never caches a local
provider (their descriptor already says `cache_status="disabled"`; the resolver now enforces it
rather than trusting the declaration), so a cache key never needs policy identity. Network
providers (Vault KV) keep the cache; it is unaffected by the file policy.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccessDenied, FileRole
from decoy_engine.secret_providers import SecretCachePolicy, admit_mask_key, default_cache
from tests.fileaccess.support import RecordingFileAccess, RootRelative

FIELD = "global_settings.mask_secret_ref"
CACHED = SecretCachePolicy(ttl_seconds=30)


@pytest.fixture
def roots(tmp_path: Path) -> Iterator[tuple[Path, RecordingFileAccess, RecordingFileAccess]]:
    a, b = tmp_path / "tenant-a", tmp_path / "tenant-b"
    a.mkdir()
    b.mkdir()
    secret = a / "k.key"
    secret.write_bytes(("ab" * 32).encode())
    os.chmod(secret, 0o600)
    pa, pb = RootRelative(a), RootRelative(b)
    try:
        yield secret, RecordingFileAccess(pa), RecordingFileAccess(pb)
    finally:
        pa.close()
        pb.close()


def _config(ref: str) -> dict:
    return {"global_settings": {"mask_secret_ref": ref}, "tables": []}


def test_a_secret_admitted_under_one_policy_is_not_served_to_a_policy_that_denies_it(
    registry, roots
) -> None:
    """A -> B interleaving: B is consulted and its denial stands; no cached value is returned."""
    secret, policy_a, policy_b = roots
    with fileaccess.scope(policy_a):
        admit_mask_key(_config(f"file:{secret}"), cache_policy=CACHED).close()
    with fileaccess.scope(policy_b), pytest.raises(FileAccessDenied):
        admit_mask_key(_config(f"file:{secret}"), cache_policy=CACHED)
    assert policy_b.requests == [(FileRole.SECRET, "read", FIELD, "denied")]


def test_every_admission_of_a_file_secret_reads_the_file(registry, roots) -> None:
    """A -> A: with a TTL, the second admission still reads through the policy (no cache)."""
    secret, policy_a, _ = roots
    with fileaccess.scope(policy_a):
        for _ in range(2):
            admit_mask_key(_config(f"file:{secret}"), cache_policy=CACHED).close()
    assert policy_a.requests == [(FileRole.SECRET, "read", FIELD, "granted")] * 2
    assert len(default_cache()) == 0


def test_an_env_secret_is_never_cached(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECOY_E3_SECRET", "ab" * 32)
    first = admit_mask_key(_config("env:DECOY_E3_SECRET"), cache_policy=CACHED)
    monkeypatch.setenv("DECOY_E3_SECRET", "cd" * 32)
    second = admit_mask_key(_config("env:DECOY_E3_SECRET"), cache_policy=CACHED)
    try:
        assert first.key_provider is not None and second.key_provider is not None
        assert first.key_provider.mask_key() != second.key_provider.mask_key()
    finally:
        first.close()
        second.close()
    assert len(default_cache()) == 0

"""Shared witnesses and protocol-conforming test providers for the F13 acceptance suite.

The providers here implement ``secret-provider/v1`` for real (they are not SDK fakes); they
exist so admission ordering can be proven for failure classes a local env/file provider cannot
produce (denied, deleted version, expired, leased). Adapter behavior is proven against a real
Vault service in ``test_vault_live.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.config import PipelineConfig
from decoy_engine.secret_providers import (
    API_VERSION,
    PreparedSecret,
    ResolvedSecret,
    SecretAccessDenied,
    SecretBuffer,
    SecretCachePolicy,
    SecretDescriptor,
    SecretExpired,
    SecretRef,
    SecretVersionUnavailable,
    prepare_secret,
)

ENGINE_VERSION = "f13-accept"
GOOD_SECRET = b"f13-throwaway-secret-0123456789ab"  # 33 bytes


def keyed_config(tmp_path: Any, *, mask_secret_ref: str | None = None, seed: int = 7) -> dict:
    gs: dict = {"seed": seed}
    if mask_secret_ref is not None:
        gs["mask_secret_ref"] = mask_secret_ref
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": gs,
            "sources": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "t.csv")}},
            "tables": [
                {"name": "t", "columns": [{"name": "email", "strategy": "hash", "namespace": "e"}]}
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
        }
    ).model_dump()


def unkeyed_config(tmp_path: Any) -> dict:
    cfg = keyed_config(tmp_path)
    cfg["tables"][0]["columns"] = [{"name": "email", "strategy": "redact"}]
    return cfg


def one_chunk(n: int = 8) -> pa.Table:
    return pa.Table.from_pandas(
        pd.DataFrame({"email": [f"user{i}@example.com" for i in range(n)]}), preserve_index=False
    )


class TouchedSource:
    """An iterable that records any touch; admission must win before it is iterated."""

    def __init__(self, chunks: list[pa.Table] | None = None) -> None:
        self.iter_calls = 0
        self.next_calls = 0
        self._chunks = chunks if chunks is not None else [one_chunk()]

    def __iter__(self) -> Iterator[pa.Table]:
        self.iter_calls += 1
        return self

    def __next__(self) -> pa.Table:
        self.next_calls += 1
        if not self._chunks:
            raise StopIteration
        return self._chunks.pop(0)

    @property
    def touched(self) -> int:
        return self.iter_calls + self.next_calls


class ProtocolProvider:
    """A conforming provider with scripted behavior and exact call accounting."""

    api_version = API_VERSION

    def __init__(
        self,
        scheme: str = "test",
        *,
        value: bytes = GOOD_SECRET,
        fail: type[Exception] | None = None,
        expires_at: float | None = None,
        renewable: bool = False,
        key_version: str = "v1",
        drift: bool = False,
    ) -> None:
        self.scheme = scheme
        self.value = value
        self.fail = fail
        self.expires_at = expires_at
        self.renewable = renewable
        self.key_version = key_version
        self.drift = drift
        self.describe_calls = 0
        self.resolve_calls = 0
        self._lock = threading.Lock()

    def describe(self, ref: SecretRef) -> SecretDescriptor:
        self.describe_calls += 1
        return SecretDescriptor(
            scheme=self.scheme, key_version=self.key_version, renewable=self.renewable
        )

    def resolve(self, ref: SecretRef, context: Any = None) -> ResolvedSecret:
        with self._lock:
            self.resolve_calls += 1
            n = self.resolve_calls
        if self.fail is not None:
            raise self.fail()
        value = self.value if not (self.drift and n > 1) else b"drifted-" + self.value
        return ResolvedSecret(
            SecretBuffer(value), key_version=self.key_version, expires_at=self.expires_at
        )

    def preflight(
        self, ref: SecretRef, requirements: Any, cache_policy: SecretCachePolicy
    ) -> PreparedSecret:
        return prepare_secret(
            provider=self, ref=ref, requirements=requirements, cache_policy=cache_policy
        )


FAILURE_CLASSES: dict[str, type[Exception]] = {
    "secret_access_denied": SecretAccessDenied,
    "secret_version_unavailable": SecretVersionUnavailable,
    "secret_expired": SecretExpired,
}


class FakeClock:
    def __init__(self, mono: float = 1000.0, wall: float = 1_700_000_000.0) -> None:
        self.mono = mono
        self.wall_now = wall

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> float:
        return self.wall_now

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall_now += seconds

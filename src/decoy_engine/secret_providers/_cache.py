"""Optional bounded process cache for prepared secrets.

Disabled unless a policy sets a TTL (1..60 s). Keys are per-process salted hashes
(``SecretRef.get_cache_key_component``), never raw references. The deadline is fixed at insert
(no sliding extension) as ``min(ttl, provider_expiry - EXPIRY_SKEW_SECONDS)`` on an injected
monotonic clock; expired, failed or invalidated entries are closed (best-effort wiped) and a
stale value is never returned as a fallback. Concurrent loads of one key are single-flighted so
the provider is read once. Pattern: a TTL cache with per-key locks (Go ``singleflight`` shape).
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Callable
from dataclasses import dataclass

from decoy_engine.secret_providers._types import EXPIRY_SKEW_SECONDS, Clock, PreparedSecret


@dataclass
class _Entry:
    master: PreparedSecret
    deadline: float


class SecretCache:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, threading.Lock] = {}

    def _evict_locked(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            entry.master.close()

    def get_or_load(
        self, key: str, *, ttl: int, clock: Clock, loader: Callable[[], PreparedSecret]
    ) -> PreparedSecret:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            now = clock.monotonic()
            with self._guard:
                entry = self._entries.get(key)
                if entry is not None and now < entry.deadline:
                    return entry.master.clone()
                self._evict_locked(key)
            prepared = loader()
            remaining = float(ttl)
            if prepared.expires_at is not None:
                remaining = min(remaining, prepared.expires_at - clock.wall() - EXPIRY_SKEW_SECONDS)
            if remaining > 0:
                with self._guard:
                    self._entries[key] = _Entry(prepared.clone(), now + remaining)
            return prepared

    def invalidate(self, key: str) -> None:
        with self._guard:
            self._evict_locked(key)

    def clear(self) -> None:
        with self._guard:
            for key in list(self._entries):
                self._evict_locked(key)

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)


_DEFAULT = SecretCache()
atexit.register(_DEFAULT.clear)


def default_cache() -> SecretCache:
    return _DEFAULT

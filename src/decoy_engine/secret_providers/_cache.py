"""Optional bounded process cache for prepared secrets.

Disabled unless a policy sets a TTL (1..60 s). Keys are per-process salted hashes
(``SecretRef.get_cache_key_component``), never raw references, scoped to the identity of the
objects that own the secret (the provider instance and the admitting registry): an entry stores
strong references to its owners and is served only to those same objects, so a recycled ``id``
can never alias another tenant's provider. The deadline is fixed at insert (no sliding
extension) as ``min(ttl, provider_expiry - EXPIRY_SKEW_SECONDS)`` on the inserting policy's
monotonic clock; expired, failed, evicted or invalidated entries are closed (best-effort wiped)
and a stale value is never returned as a fallback.

Invalidation is linearizable against in-flight loads: ``clear`` bumps a global epoch and
``invalidate`` bumps the epoch of that key's live flight; a loader records both when its read
starts and refuses to insert if either moved, so material read before a revocation is returned to
its own caller but never cached. A hit also honors the CALLING policy: an entry older than the
caller's TTL is not served to it.

Bounds: at most ``capacity`` entries (oldest insert evicted first), every expired entry is pruned
on each access, and single-flight locks are reference-counted and dropped when their last caller
leaves, so rotating references cannot grow the cache or the lock table. Concurrent loads of one
key are single-flighted so the provider is read once. Pattern: a TTL cache with per-key locks
(Go ``singleflight`` shape, ``cachetools.TTLCache`` maxsize/expire-on-access bounds).
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Callable
from dataclasses import dataclass

from decoy_engine.secret_providers._types import EXPIRY_SKEW_SECONDS, Clock, PreparedSecret

MAX_CACHE_ENTRIES = 128

_Key = tuple[str, ...]


@dataclass
class _Entry:
    master: PreparedSecret
    deadline: float
    clock: Clock
    owners: tuple[object, ...]
    inserted: float

    def expired(self) -> bool:
        return self.clock.monotonic() >= self.deadline

    def older_than(self, ttl: float) -> bool:
        return self.clock.monotonic() - self.inserted >= ttl


@dataclass
class _Flight:
    lock: threading.Lock
    callers: int = 0
    epoch: int = 0


class SecretCache:
    def __init__(self, capacity: int = MAX_CACHE_ENTRIES) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("cache capacity must be a positive integer")
        self._capacity = capacity
        self._guard = threading.Lock()
        self._entries: dict[_Key, _Entry] = {}
        self._locks: dict[_Key, _Flight] = {}
        self._epoch = 0

    @staticmethod
    def _key(key: str, owners: tuple[object, ...]) -> _Key:
        return (key, *(str(id(owner)) for owner in owners))

    def _evict_locked(self, key: _Key) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            entry.master.close()

    def _prune_locked(self) -> None:
        for key in [k for k, entry in self._entries.items() if entry.expired()]:
            self._evict_locked(key)

    def _insert_locked(self, key: _Key, entry: _Entry) -> None:
        self._evict_locked(key)
        while len(self._entries) >= self._capacity:
            self._evict_locked(next(iter(self._entries)))
        self._entries[key] = entry

    def _hit_locked(
        self, key: _Key, owners: tuple[object, ...], ttl: float
    ) -> PreparedSecret | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expired() or any(a is not b for a, b in zip(entry.owners, owners, strict=True)):
            self._evict_locked(key)
            return None
        if entry.older_than(ttl):
            return None  # valid for a longer-TTL caller, so it stays; this caller reloads
        return entry.master.clone()

    def get_or_load(
        self,
        key: str,
        *,
        owners: tuple[object, ...],
        ttl: int,
        clock: Clock,
        loader: Callable[[], PreparedSecret],
    ) -> PreparedSecret:
        full = self._key(key, owners)
        with self._guard:
            self._prune_locked()
            flight = self._locks.setdefault(full, _Flight(threading.Lock()))
            flight.callers += 1
        try:
            with flight.lock:
                with self._guard:
                    hit = self._hit_locked(full, owners, float(ttl))
                    started = (self._epoch, flight.epoch)
                if hit is not None:
                    return hit
                now = clock.monotonic()
                prepared = loader()
                remaining = float(ttl)
                if prepared.expires_at is not None:
                    remaining = min(
                        remaining, prepared.expires_at - clock.wall() - EXPIRY_SKEW_SECONDS
                    )
                if remaining > 0:
                    entry = _Entry(prepared.clone(), now + remaining, clock, owners, now)
                    with self._guard:
                        if started == (self._epoch, flight.epoch):
                            self._insert_locked(full, entry)
                        else:
                            entry.master.close()
                return prepared
        finally:
            with self._guard:
                flight.callers -= 1
                if flight.callers == 0 and self._locks.get(full) is flight:
                    del self._locks[full]

    def invalidate(self, key: str, *, owners: tuple[object, ...]) -> None:
        full = self._key(key, owners)
        with self._guard:
            flight = self._locks.get(full)
            if flight is not None:
                flight.epoch += 1
            self._evict_locked(full)

    def clear(self) -> None:
        with self._guard:
            self._epoch += 1
            for key in list(self._entries):
                self._evict_locked(key)

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)


_DEFAULT = SecretCache()
atexit.register(_DEFAULT.clear)


def default_cache() -> SecretCache:
    return _DEFAULT

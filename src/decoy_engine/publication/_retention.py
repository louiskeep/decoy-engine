"""Rejected-staging retention and lifecycle outcomes (opt-in, finite, private).

Default behavior is best-effort discard with a typed report. `retain_rejected` is only
honored for an authorized private store (owned by this uid, mode 0700), inside a byte
budget, with a finite expiry; retention metadata is separate from publication descriptors
and a retained descriptor is neutralized so it can never acquire a binding. No sweeper:
cleanup is an explicit, ownership-verified call on an exact contained target. Unlinking
is not secure erasure and is never reported as such.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from decoy_engine.publication import _codec
from decoy_engine.publication._types import RetentionPolicy, SideStatus

META_NAME = "meta.json"
PAYLOAD_NAME = "payload"
HOLDS_NAME = "holds"
REJECTED_SUFFIX = ".rejected"
_ID_SAFE = set("0123456789abcdef")
HOLD_KINDS = frozenset({"reader", "history", "backup", "active_attempt"})


@dataclass(frozen=True)
class AbortReport:
    """What happened to a rejected/aborted attempt's staging. Never rewrites publication truth."""

    status: SideStatus
    reason: str | None = None
    record_id: str | None = None
    retained_path: Path | None = None
    resource: Path | None = None  # recoverable record when cleanup failed
    classification: str = "rejected_staging"


@dataclass(frozen=True)
class RetainedRecord:
    record_id: str
    path: Path
    owner: str
    operation_id: str
    attempt_id: str
    reason_code: str
    classification: str
    expires_at: float
    holds: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class CleanupResult:
    status: SideStatus
    reason: str | None = None


def _private_store(store: Path) -> bool:
    try:
        st = os.lstat(store)
    except OSError:
        return False
    return (
        stat.S_ISDIR(st.st_mode)
        and st.st_uid == os.getuid()
        and stat.S_IMODE(st.st_mode) & 0o077 == 0
    )


def _dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def _store_bytes(store: Path) -> int:
    return _dir_bytes(store)


def discard(path: Path, reason: str | None) -> AbortReport:
    if not path.exists() and not path.is_symlink():
        return AbortReport("succeeded", reason)
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        return AbortReport("failed", reason, resource=path)
    return AbortReport("succeeded", reason)


def dispose_staging(
    staging: Path,
    policy: RetentionPolicy | None,
    *,
    operation_id: str,
    attempt_id: str,
    reason: str,
) -> AbortReport:
    """Retain (when authorized and within budget) or discard a rejected attempt's staging."""
    if policy is None:
        return discard(staging, reason)
    if not staging.exists():
        return AbortReport("succeeded", reason)
    if not _private_store(policy.store):
        return _failed_retention(staging, "retention_store_not_private")
    if _store_bytes(policy.store) + _dir_bytes(staging) > policy.byte_budget:
        return _failed_retention(staging, "retention_budget_exceeded")
    if policy.expiry_s <= 0:
        return _failed_retention(staging, "retention_expiry_not_finite")
    record_id = uuid.uuid4().hex
    record = policy.store / record_id
    try:
        record.mkdir(mode=0o700)
        payload = record / PAYLOAD_NAME
        os.rename(staging, payload)  # same filesystem or refuse: no copy fallback
        descriptor = payload / _codec.DESCRIPTOR_NAME
        if descriptor.exists():  # a retained claim must never resolve as committed
            os.rename(descriptor, payload / (_codec.DESCRIPTOR_NAME + REJECTED_SUFFIX))
        (record / HOLDS_NAME).mkdir(mode=0o700)
        (record / META_NAME).write_bytes(
            _codec.canonical_bytes(
                {
                    "owner": policy.owner,
                    "operation_id": operation_id,
                    "attempt_id": attempt_id,
                    "reason_code": reason,
                    "classification": "rejected_staging",
                    "expires_at": time.time() + policy.expiry_s,
                }
            )
        )
        os.chmod(record / META_NAME, 0o600)
    except OSError:
        shutil.rmtree(record, ignore_errors=True)
        return _failed_retention(staging, "retention_move_failed")
    return AbortReport("retained", reason, record_id, payload)


def _failed_retention(staging: Path, why: str) -> AbortReport:
    report = discard(staging, why)
    return AbortReport("failed", why, resource=report.resource)


def _record_dir(store: Path, record_id: str) -> Path | None:
    if not record_id or set(record_id) - _ID_SAFE:
        return None
    path = store / record_id
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):  # never follow a symlink out of the store
        return None
    return path


def _load(store: Path, record_id: str) -> RetainedRecord | None:
    path = _record_dir(store, record_id)
    if path is None:
        return None
    try:
        meta = _codec.parse_strict((path / META_NAME).read_bytes())
        holds = tuple(sorted(p.name for p in (path / HOLDS_NAME).iterdir()))
        return RetainedRecord(
            record_id,
            path,
            meta["owner"],
            meta["operation_id"],
            meta["attempt_id"],
            meta["reason_code"],
            meta["classification"],
            float(meta["expires_at"]),
            holds,
        )
    except (OSError, KeyError, ValueError, _codec.CodecError):
        return None


def inventory(store: Path) -> list[RetainedRecord]:
    """Read-only seam for F25/F26: every well-formed retained record in `store`."""
    if not store.is_dir():
        return []
    found = (_load(store, p.name) for p in sorted(store.iterdir()))
    return [r for r in found if r is not None]


def add_hold(store: Path, record_id: str, kind: str, ref: str) -> bool:
    record = _load(store, record_id)
    if record is None or kind not in HOLD_KINDS:
        return False
    (record.path / HOLDS_NAME / f"{kind}-{ref}").write_text(ref)
    return True


def remove_hold(store: Path, record_id: str, kind: str, ref: str) -> bool:
    record = _load(store, record_id)
    if record is None:
        return False
    (record.path / HOLDS_NAME / f"{kind}-{ref}").unlink(missing_ok=True)
    return True


def cleanup_retained(
    store: Path, record_id: str, *, owner: str, now: float | None = None
) -> CleanupResult:
    """Delete one expired, unheld, owner-verified retained record at its exact path."""
    record = _load(store, record_id)
    if record is None:
        return CleanupResult("failed", "unknown_or_unsafe_record")
    if record.owner != owner:
        return CleanupResult("failed", "ownership_mismatch")
    if record.holds:
        return CleanupResult("retained", "held")
    if (time.time() if now is None else now) < record.expires_at:
        return CleanupResult("retained", "not_expired")
    shutil.rmtree(record.path, ignore_errors=True)
    if record.path.exists():
        return CleanupResult("failed", "cleanup_failed")  # retryable; record is intact
    return CleanupResult("succeeded")

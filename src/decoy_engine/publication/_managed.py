"""Pattern: Iceberg atomic metadata-pointer switch over immutable files (Apache Iceberg spec, Apache-2.0). See: https://iceberg.apache.org/spec/.

`managed_dataset/v1`: immutable generations under a dataset root, one `CURRENT` pointer.

Methodology: Apache Iceberg writes immutable data/metadata and switches a single table
metadata reference atomically; here the reference is a small `CURRENT` file replaced with
write-temp + fsync + `os.replace` + directory fsync. The previous generation is never
modified, so a reader that resolved `CURRENT` once keeps a consistent snapshot, and a
crash before the pointer switch leaves the old result current and a harmless, unreferenced
generation. Replacement is never `old -> backup; staging -> old` (that has a missing-target
interval). Protected backup is a private verified copy plus a hold marker.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decoy_engine.errors import DecoyError
from decoy_engine.publication import _codec
from decoy_engine.publication._local_directory import (
    ExistingState,
    _receipt,
    _refuse,
    _reverify_staged_claim,
    context_matches,
    fsync_dir,
    gate_request,
    inspect_destination,
    prepare_directory,
    write_private_file,
)
from decoy_engine.publication._lock import DestinationLock, LockUnavailableError
from decoy_engine.publication._measure import MeasureError, hash_file, verify_artifacts
from decoy_engine.publication._retention import CleanupResult
from decoy_engine.publication._types import (
    UNIT_MANAGED_DATASET,
    ArtifactRef,
    CommitReceipt,
    CommitUncertainError,
    NoBackup,
    PreparedPublication,
    ProtectedBackup,
    PublicationRefusedError,
    PublicationRequest,
    Replace,
)

CURRENT_NAME = "CURRENT"
GENERATIONS = "generations"
BACKUPS = "backups"
STAGING = ".staging"
HOLD_NAME = "_decoy_hold.json"
_POINTER_SCHEMA = "decoy-current/v1"
_PRIVATE = 0o700


class ResolveError(DecoyError):
    """`CURRENT` or the generation it names is missing, malformed or fails verification."""


@dataclass(frozen=True)
class ResolvedGeneration:
    """A pinned, immutable snapshot: read every table from `path`, never from the root."""

    path: Path
    descriptor_digest: str
    result_id: str
    artifacts: tuple[ArtifactRef, ...]
    doc: Mapping[str, Any]


def staging_root(root: Path) -> Path:
    return root / STAGING


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, _PRIVATE)


def _pointer_bytes(descriptor_digest: str, result_id: str) -> bytes:
    return _codec.canonical_bytes(
        {
            "schema": _POINTER_SCHEMA,
            "descriptor_digest": descriptor_digest,
            "result_id": result_id,
            "generation": f"{GENERATIONS}/{descriptor_digest}",
        }
    )


def _read_pointer(root: Path) -> dict[str, Any] | None:
    path = root / CURRENT_NAME
    if not os.path.lexists(path):
        return None
    try:
        doc = _codec.parse_strict(path.read_bytes())
        digest, result_id = doc.get("descriptor_digest"), doc.get("result_id")
        if (
            doc.get("schema") != _POINTER_SCHEMA
            or not isinstance(digest, str)
            or not isinstance(result_id, str)
            or doc.get("generation") != f"{GENERATIONS}/{digest}"
            or _codec.canonical_bytes(doc) != path.read_bytes()
        ):
            raise _codec.CodecError("malformed CURRENT pointer")
    except (OSError, _codec.CodecError) as exc:
        raise ResolveError(f"CURRENT is not a valid pointer: {exc}") from exc
    return doc


def _generation_state(root: Path, digest: str) -> ExistingState:
    state = inspect_destination(root / GENERATIONS / digest)
    if state.kind == "committed" and state.descriptor_digest != digest:
        return ExistingState("corrupt", "generation_name_mismatch")
    return state


def resolve_current(root: Path) -> ResolvedGeneration | None:
    """Resolve `CURRENT` once and verify the generation it pins; None if never published."""
    pointer = _read_pointer(root)
    if pointer is None:
        return None
    state = _generation_state(root, pointer["descriptor_digest"])
    if state.kind != "committed" or state.doc is None:
        raise ResolveError(f"CURRENT names an unverifiable generation: {state.kind} {state.reason}")
    if state.doc["result_id"] != pointer["result_id"]:
        raise ResolveError("CURRENT result_id does not match its generation")
    return ResolvedGeneration(
        root / GENERATIONS / pointer["descriptor_digest"],
        pointer["descriptor_digest"],
        state.doc["result_id"],
        state.entries,
        state.doc,
    )


def _history_descriptor(root: Path, digest: str) -> tuple[dict[str, Any], str] | None:
    """Parse a (possibly payload-expired) historical descriptor; payload is not verified."""
    path = root / GENERATIONS / digest / _codec.DESCRIPTOR_NAME
    try:
        raw = path.read_bytes()
        doc = _codec.parse_descriptor(raw)
    except (OSError, _codec.CodecError):
        return None
    if _codec.sha256_hex(raw) != digest:
        return None
    return doc, digest


def _lineage_match(root: Path, start: str, operation_id: str) -> tuple[dict[str, Any], str] | None:
    digest: str | None = start
    seen: set[str] = set()
    while digest and digest not in seen:
        seen.add(digest)
        found = _history_descriptor(root, digest)
        if found is None:
            return None
        doc, _ = found
        if doc["operation_id"] == operation_id:
            return found
        digest = doc.get("lineage", {}).get("predecessor_digest")
    return None


def _receipt_for(
    request: PublicationRequest, doc: Mapping[str, Any], digest: str, *, current: bool, ev: str
) -> CommitReceipt:
    return _receipt(
        request,
        "already_committed",
        result_id=doc["result_id"],
        descriptor_digest=digest,
        evidence_digest=ev,
        artifacts=doc["_entries"],
        is_current=current,
        visible=True,
        durable=_sync(root_of(request)),
        generation=f"{GENERATIONS}/{digest}",
    )


def root_of(request: PublicationRequest) -> Path:
    return request.destination


def _sync(root: Path) -> bool:
    try:
        fsync_dir(root)
    except OSError:
        return False
    return True


def lookup_committed_managed(request: PublicationRequest) -> CommitReceipt | None:
    root = request.destination
    gate_request(request)
    try:
        cur = resolve_current(root)
    except ResolveError:
        return None
    if cur is None:
        return None
    if (
        cur.doc["operation_id"] == request.operation_id
        or request.expected_result_id == cur.result_id
    ):
        if request.expected_result_id not in (None, cur.result_id):
            return None
        ev = cur.doc["evidence"]["sha256"]
        _require_context(request, cur.path, ev)
        _check_lineage(request, cur.doc, cur.descriptor_digest, root)
        return _receipt_for(request, cur.doc, cur.descriptor_digest, current=True, ev=ev)
    if request.expected_result_id is not None:
        return None
    found = _lineage_match(root, cur.descriptor_digest, request.operation_id)
    if found is None:
        return None
    _require_context(request, root / GENERATIONS / found[1], found[0]["evidence"]["sha256"])
    _check_lineage(request, found[0], found[1], root)
    return _historical_receipt(request, *found)


def _check_lineage(
    request: PublicationRequest, doc: Mapping[str, Any], digest: str, root: Path
) -> None:
    """A replacement request may only be answered `already_committed` by a result that was
    committed over the predecessor it authorized, with the backup it required."""
    ow = request.overwrite
    if not isinstance(ow, Replace):
        return
    lineage = doc.get("lineage", {})
    if digest == ow.expected_descriptor_digest:
        return  # replacing a result with identical content: the CAS is satisfied, nothing to do
    if lineage.get("predecessor_digest") != ow.expected_descriptor_digest:
        raise _refuse(request, "conflict", "stale_predecessor")
    if isinstance(ow.backup, ProtectedBackup):
        held = inspect_destination(root / BACKUPS / ow.expected_descriptor_digest)
        if (
            lineage.get("backup_ref") != ow.backup.policy_ref
            or held.kind != "committed"
            or held.descriptor_digest != ow.expected_descriptor_digest
        ):
            raise _refuse(request, "conflict", "backup_missing")


def _require_context(request: PublicationRequest, directory: Path, evidence_sha: str) -> None:
    if not context_matches(directory, evidence_sha, request):
        raise _refuse(request, "conflict", "acceptance_context_differs")


def _historical_receipt(
    request: PublicationRequest, doc: dict[str, Any], digest: str
) -> CommitReceipt:
    """A superseded operation: report it if its payload still verifies, never replay it."""
    root = request.destination
    try:
        verify_artifacts(root / GENERATIONS / digest, doc["_entries"])
    except MeasureError as exc:
        raise _refuse(request, "rejected", "committed_artifacts_expired") from exc
    return _receipt_for(request, doc, digest, current=False, ev=doc["evidence"]["sha256"])


def _judge(
    prepared: PreparedPublication, cur: ResolvedGeneration | None
) -> CommitReceipt | PublicationRefusedError | None:
    request = prepared.request
    root = request.destination
    if cur is None:
        if isinstance(request.overwrite, Replace):
            return _refuse(request, "conflict", "predecessor_missing")
        return None
    cur_ev = cur.doc["evidence"]["sha256"]
    same_content = cur.result_id == prepared.result_id and cur_ev == prepared.evidence_digest
    if same_content:
        _check_lineage(request, cur.doc, cur.descriptor_digest, root)
        return _receipt_for(request, cur.doc, cur.descriptor_digest, current=True, ev=cur_ev)
    if cur.doc["operation_id"] == request.operation_id:
        return _refuse(request, "conflict", "operation_conflict")
    older = _lineage_match(root, cur.descriptor_digest, request.operation_id)
    if older is not None:
        odoc, odigest = older
        if (
            odoc["result_id"] == prepared.result_id
            and odoc["evidence"]["sha256"] == prepared.evidence_digest
        ):
            return _historical_receipt(request, odoc, odigest)  # superseded retry: never replayed
        return _refuse(request, "conflict", "operation_conflict")
    if not isinstance(request.overwrite, Replace):
        return _refuse(request, "conflict", "target_occupied_different")
    if request.overwrite.expected_descriptor_digest != cur.descriptor_digest:
        return _refuse(request, "conflict", "stale_predecessor")
    return None


def _backup_step(name: str) -> None:
    """Fault-injection seam: tests raise here to exercise every backup boundary."""


def ensure_protected_backup(root: Path, prior: ResolvedGeneration, policy_ref: str) -> str:
    """Private verified copy of the prior generation plus a hold; reused when it verifies."""
    dest = root / BACKUPS / prior.descriptor_digest
    if dest.exists():
        state = inspect_destination(dest)
        if state.kind == "committed" and state.descriptor_digest == prior.descriptor_digest:
            return f"{BACKUPS}/{prior.descriptor_digest}"
        shutil.rmtree(dest, ignore_errors=True)  # an unverifiable leftover is not a backup
    tmp = staging_root(root) / f"backup-{uuid.uuid4().hex}"
    try:
        _private_dir(tmp)
        for name in sorted(p.name for p in prior.path.iterdir()):
            _backup_step(f"copy:{name}")
            shutil.copyfile(prior.path / name, tmp / name)
            os.chmod(tmp / name, 0o600)
            fd = os.open(tmp / name, os.O_RDONLY)
            try:
                _backup_step(f"fsync:{name}")
                os.fsync(fd)
            finally:
                os.close(fd)
        _backup_step("verify")
        verify_artifacts(tmp, prior.artifacts)
        if hash_file(tmp / _codec.DESCRIPTOR_NAME)[1] != prior.descriptor_digest:
            raise MeasureError("descriptor_changed", _codec.DESCRIPTOR_NAME)
        _backup_step("hold")
        write_private_file(tmp / HOLD_NAME, _codec.canonical_bytes({"policy_ref": policy_ref}))
        fsync_dir(tmp)
        _private_dir(dest.parent)
        _backup_step("publish")
        os.replace(tmp, dest)
        fsync_dir(dest.parent)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return f"{BACKUPS}/{prior.descriptor_digest}"


def commit_managed(prepared: PreparedPublication) -> CommitReceipt:
    request, root = prepared.request, prepared.request.destination
    gate_request(request)
    _private_dir(root)
    try:
        lock = DestinationLock(root)
        lock.__enter__()
    except LockUnavailableError as exc:
        raise _refuse(request, "unsupported", "lock_unavailable") from exc
    try:
        try:
            cur = resolve_current(root)
        except ResolveError as exc:
            raise _refuse(request, "conflict", "target_corrupt:current") from exc
        judged = _judge(prepared, cur)
        if isinstance(judged, CommitReceipt):
            return judged
        if judged is not None:
            raise judged
        backup_ref = None
        if cur is not None and isinstance(request.overwrite, Replace):
            if isinstance(request.overwrite.backup, ProtectedBackup):
                try:
                    backup_ref = ensure_protected_backup(
                        root, cur, request.overwrite.backup.policy_ref
                    )
                except (OSError, MeasureError) as exc:
                    raise PublicationRefusedError(
                        "protected backup failed; nothing was switched",
                        _receipt(request, "rejected", "backup_failed", backup_status="failed"),
                    ) from exc
        if request.cancelled is not None and request.cancelled():
            raise _refuse(request, "cancelled_before_commit", "cancelled")
        _reverify_staged_claim(prepared)
        return _promote_and_switch(prepared, backup_ref)
    finally:
        lock.__exit__(None, None, None)


def _promote_and_switch(prepared: PreparedPublication, backup_ref: str | None) -> CommitReceipt:
    request, root = prepared.request, prepared.request.destination
    gen_rel = f"{GENERATIONS}/{prepared.descriptor_digest}"
    bound: dict[str, Any] = dict(
        result_id=prepared.result_id,
        descriptor_digest=prepared.descriptor_digest,
        evidence_digest=prepared.evidence_digest,
        artifacts=prepared.artifacts,
        is_current=True,
        generation=gen_rel,
        backup_ref=backup_ref,
        backup_status="succeeded" if backup_ref else "not_requested",
    )
    try:
        _private_dir(root / GENERATIONS)
        os.replace(prepared.staging, root / gen_rel)  # an unreferenced generation is not a result
        fsync_dir(root / GENERATIONS)
        pointer_tmp = root / f"{CURRENT_NAME}.{uuid.uuid4().hex}.tmp"
        write_private_file(
            pointer_tmp, _pointer_bytes(prepared.descriptor_digest, prepared.result_id)
        )
    except OSError as exc:
        raise PublicationRefusedError(
            f"publication rejected: could not stage the generation: {exc}",
            _receipt(
                request,
                "rejected",
                f"promote_failed:{exc.errno}",
                backup_status=bound["backup_status"],
                backup_ref=backup_ref,
            ),
        ) from exc
    try:
        os.replace(pointer_tmp, root / CURRENT_NAME)
    except OSError as exc:
        pointer_tmp.unlink(missing_ok=True)
        after = resolve_pointer_digest(root)
        if after == prepared.descriptor_digest:
            raise CommitUncertainError(
                "pointer switch reported failure but the binding is visible",
                _receipt(
                    request, "commit_uncertain", "switch_error_visible", visible=True, **bound
                ),
            ) from exc
        raise PublicationRefusedError(
            f"publication rejected: pointer switch failed: {exc}",
            _receipt(
                request,
                "rejected",
                f"switch_failed:{exc.errno}",
                backup_status=bound["backup_status"],
                backup_ref=backup_ref,
            ),
        ) from exc
    try:
        fsync_dir(root)
    except OSError as exc:
        raise CommitUncertainError(
            "published but the dataset root could not be synced",
            _receipt(request, "commit_uncertain", "directory_sync_failed", visible=True, **bound),
        ) from exc
    return _receipt(request, "published", visible=True, durable=True, **bound)


def resolve_pointer_digest(root: Path) -> str | None:
    try:
        pointer = _read_pointer(root)
    except ResolveError:
        return None
    return pointer["descriptor_digest"] if pointer else None


def reconcile_managed(request: PublicationRequest) -> CommitReceipt | None:
    gate_request(request)
    root = request.destination
    try:
        with DestinationLock(root):
            return lookup_committed_managed(request)
    except LockUnavailableError as exc:
        raise _refuse(request, "unsupported", "lock_unavailable") from exc


def restore_backup(
    root: Path,
    backup_digest: str,
    *,
    expected_current_digest: str,
    operation_id: str | None = None,
    backup: NoBackup | ProtectedBackup = NoBackup(),  # noqa: B008
) -> CommitReceipt:
    """Restore a protected backup as a NEW authorized replacement (never an implicit rollback).

    The backup descriptor and every bound byte are verified first; a stale
    `expected_current_digest` conflicts exactly like any other replacement.
    """
    request = PublicationRequest(
        destination=root,
        unit=UNIT_MANAGED_DATASET,
        operation_id=operation_id or uuid.uuid4().hex,
        overwrite=Replace(expected_current_digest, backup),
    )
    gate_request(request)
    src = root / BACKUPS / backup_digest
    state = inspect_destination(src)
    if state.kind != "committed" or state.descriptor_digest != backup_digest:
        raise _refuse(request, "rejected", "backup_unverifiable")
    stage = staging_root(root) / f"restore-{uuid.uuid4().hex}"
    try:
        _private_dir(stage)
        for entry in state.entries:
            shutil.copyfile(src / entry.path, stage / entry.path)
        prepared = prepare_directory(stage, request)
        return commit_managed(prepared)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


HOLDS_DIR = ".holds"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _live_reader_holds(root: Path, digest: str) -> list[Path]:
    live = []
    for hold in (
        (root / HOLDS_DIR / digest).glob("*") if (root / HOLDS_DIR / digest).is_dir() else ()
    ):
        try:
            pid = int(hold.read_text())
        except (OSError, ValueError):
            live.append(hold)  # an unreadable hold is conservatively live
            continue
        if _pid_alive(pid):
            live.append(hold)
        else:
            hold.unlink(missing_ok=True)  # the reader died; its hold cannot outlive it
    return live


@contextmanager
def pin_reader(root: Path) -> Iterator[ResolvedGeneration]:
    """Resolve `CURRENT` once and hold that generation against payload expiry while read."""
    with DestinationLock(root):
        cur = resolve_current(root)
        if cur is None:
            raise ResolveError("dataset has no published result")
        hold_dir = root / HOLDS_DIR / cur.descriptor_digest
        _private_dir(hold_dir)
        hold = hold_dir / uuid.uuid4().hex
        hold.write_text(str(os.getpid()))
    try:
        yield cur
    finally:
        hold.unlink(missing_ok=True)


def expire_historical_payload(root: Path, descriptor_digest: str) -> CleanupResult:
    """Drop the payload (never the descriptor/evidence tombstone) of a superseded result.

    Refuses the current generation, anything outside the committed lineage, and anything a
    live reader pins. Afterwards a retry of that operation reports
    `committed_artifacts_expired` instead of replaying it. Protected backups live under
    `backups/` and are never touched here.
    """
    with DestinationLock(root):
        pointer = _read_pointer(root)
        if pointer is None:
            return CleanupResult("failed", "no_published_result")
        if pointer["descriptor_digest"] == descriptor_digest:
            return CleanupResult("retained", "is_current")
        digest: str | None = pointer["descriptor_digest"]
        target: dict[str, Any] | None = None
        seen: set[str] = set()
        while digest and digest not in seen:
            seen.add(digest)
            found = _history_descriptor(root, digest)
            if found is None:
                break
            if digest == descriptor_digest:
                target = found[0]
                break
            digest = found[0].get("lineage", {}).get("predecessor_digest")
        if target is None:
            return CleanupResult("failed", "not_in_committed_lineage")
        if _live_reader_holds(root, descriptor_digest):
            return CleanupResult("retained", "reader_hold")
        gen = root / GENERATIONS / descriptor_digest
        for entry in target["_entries"]:
            (gen / entry.path).unlink(missing_ok=True)
        return CleanupResult("succeeded")

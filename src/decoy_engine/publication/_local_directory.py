"""Pattern: rename(2) atomic replace + fsync(2) ordering + Delta transaction-identifier retry (POSIX.1-2008; Delta PROTOCOL.md, Apache-2.0). See: https://man7.org/linux/man-pages/man2/rename.2.html.

`immutable_directory/v1`: stage a complete unit, then bind it with one rename.

Methodology: rename(2) atomically replaces a name (an empty directory target is
replaceable, a non-empty one is not); fsync(2) of a file does not persist its directory
entry, so payloads are synced, then staging directories, then the destination parent
after the rename. Idempotence follows Delta's "read committed identity before retrying"
and Git's content addressing: the committed binding is the descriptor inside the
renamed directory, and a retry is judged against it under the destination lock.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from decoy_engine.publication import _codec
from decoy_engine.publication import _protocol as _proto
from decoy_engine.publication._lock import DestinationLock, LockTimeoutError, LockUnavailableError
from decoy_engine.publication._measure import (
    MeasureError,
    hash_file,
    measure_directory,
    verify_artifacts,
)
from decoy_engine.publication._tokens import os_error_token
from decoy_engine.publication._types import (
    UNIT_MANAGED_DATASET,
    ArtifactRef,
    CommitReceipt,
    CommitUncertainError,
    Outcome,
    PreparedPublication,
    ProtectedBackup,
    PublicationRefusedError,
    PublicationRequest,
    Replace,
    SideStatus,
)

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _receipt(
    request: PublicationRequest,
    outcome: Outcome,
    reason: str | None = None,
    **fields: Any,
) -> CommitReceipt:
    if "artifacts" in fields:  # receipts report summaries, never artifact names
        fields["artifacts"] = tuple(a.summary() for a in fields["artifacts"])
    return CommitReceipt(
        outcome=outcome,
        operation_id=request.operation_id,
        reason_code=reason,
        **fields,
    )


def _refuse(request: PublicationRequest, outcome: Outcome, reason: str) -> PublicationRefusedError:
    return PublicationRefusedError(
        f"publication {outcome}: {reason}", _receipt(request, outcome, reason)
    )


def gate_request(request: PublicationRequest) -> None:
    """Line 1 of every request-taking operation: admission, then the acceptance gate."""
    _proto.admit_request(request)
    reject_failed_checks(request)


def validate_request(request: PublicationRequest) -> None:
    """Everything decidable before the first output write; refuses, never degrades."""
    gate_request(request)
    if request.required_free_bytes is not None:
        parent = request.destination.parent
        probe = parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if shutil.disk_usage(probe).free < request.required_free_bytes:
            raise _refuse(request, "rejected", "insufficient_budget")
    try:
        with DestinationLock(request.destination, timeout_s=0.0):
            pass
    except LockTimeoutError:
        return  # locking is supported; a peer merely holds it right now
    except LockUnavailableError as exc:
        raise _refuse(request, "unsupported", "lock_unavailable") from exc


def _lineage(request: PublicationRequest) -> tuple[str | None, str | None]:
    """Managed replacement records its predecessor and protected backup in the descriptor."""
    if request.unit != UNIT_MANAGED_DATASET or not isinstance(request.overwrite, Replace):
        return None, None
    ref = (
        request.overwrite.backup.policy_ref
        if isinstance(request.overwrite.backup, ProtectedBackup)
        else None
    )
    return request.overwrite.expected_descriptor_digest, ref


def reject_failed_checks(request: PublicationRequest) -> None:
    """A required failed/inconclusive check denies publication AND recovery, up front."""
    for check in request.checks:
        if check.required and check.disposition in ("fail", "inconclusive"):
            raise _refuse(request, "rejected", "required_check_failed")


def context_matches(directory: Path, expected_sha: str, request: PublicationRequest) -> bool:
    """The committed evidence core (hash-bound by the descriptor) carries the same
    acceptance context as `request`."""
    path = directory / _codec.EVIDENCE_NAME
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    return _codec.sha256_hex(raw) == expected_sha and _codec.evidence_matches_context(
        raw, request.validation_policy_fingerprint, request.checks
    )


def prepare_directory(
    stage: Path,
    request: PublicationRequest,
    *,
    producer_rows: Mapping[str, int] | None = None,
) -> PreparedPublication:
    gate_request(request)
    try:
        entries = measure_directory(stage, producer_rows=producer_rows)
    except MeasureError as exc:
        raise _refuse(request, "rejected", exc.reason_code) from exc
    if request.expected_tables is not None:
        got = {e.path.removesuffix(".parquet") for e in entries if e.role == "table"}
        want = set(request.expected_tables)
        if want - got:
            raise _refuse(request, "rejected", "missing_expected_table")
        if got - want:
            raise _refuse(request, "rejected", "unexpected_table")
    predecessor, backup_ref = _lineage(request)
    result_id = _codec.compute_result_id(entries)
    evidence = _codec.evidence_core_bytes(
        result_id=result_id,
        policy_fingerprint=request.validation_policy_fingerprint,
        checks=request.checks,
        inventory_digest=_codec.sha256_hex(_codec.canonical_bytes([e.sha256 for e in entries])),
    )
    descriptor = _codec.descriptor_bytes(
        unit=request.unit,
        destination=str(request.destination.resolve()),
        job_id=request.job_id,
        operation_id=request.operation_id,
        attempt_id=request.attempt_id,
        entries=entries,
        policy_fingerprint=request.validation_policy_fingerprint,
        evidence=evidence,
        predecessor_digest=predecessor,
        backup_ref=backup_ref,
    )
    for e in entries:
        _fsync_file(stage / e.path)
    write_private_file(stage / _codec.EVIDENCE_NAME, evidence)
    write_private_file(stage / _codec.DESCRIPTOR_NAME, descriptor)
    fsync_dir(stage)
    return PreparedPublication(
        request=request,
        staging=stage,
        artifacts=entries,
        result_id=result_id,
        descriptor_digest=_codec.sha256_hex(descriptor),
        evidence_digest=_codec.sha256_hex(evidence),
    )


@dataclass(frozen=True)
class ExistingState:
    kind: str  # absent | empty | committed | legacy | not_directory | corrupt
    reason: str | None = None
    doc: Mapping[str, Any] | None = None
    descriptor_digest: str | None = None
    evidence_digest: str | None = None

    @property
    def entries(self) -> tuple[ArtifactRef, ...]:
        return self.doc["_entries"] if self.doc else ()


def inspect_destination(target: Path) -> ExistingState:
    """Resolve the committed binding at `target`, verifying every bound artifact."""
    if not os.path.lexists(target):
        return ExistingState("absent")
    if target.is_symlink() or not target.is_dir():
        return ExistingState("not_directory")
    names = {p.name for p in target.iterdir()}
    if not names:
        return ExistingState("empty")
    if _codec.DESCRIPTOR_NAME not in names:
        return ExistingState("legacy")
    try:
        raw = (target / _codec.DESCRIPTOR_NAME).read_bytes()
        doc = _codec.parse_descriptor(raw)
    except (OSError, _codec.CodecError):
        return ExistingState("corrupt", "descriptor_invalid")
    try:
        ev_size, ev_digest = hash_file(target / _codec.EVIDENCE_NAME)
    except OSError:
        return ExistingState("corrupt", "evidence_missing")
    if ev_digest != doc["evidence"]["sha256"] or ev_size != doc["evidence"]["size"]:
        return ExistingState("corrupt", "evidence_changed")
    try:
        verify_artifacts(target, doc["_entries"])
    except MeasureError as exc:
        return ExistingState("corrupt", exc.reason_code)
    return ExistingState("committed", None, doc, _codec.sha256_hex(raw), ev_digest)


def _sync_parent(target: Path) -> bool:
    try:
        fsync_dir(target.parent)
    except OSError:
        return False
    return True


def _existing_receipt(
    request: PublicationRequest, ex: ExistingState, *, durable: bool
) -> CommitReceipt:
    return _receipt(
        request,
        "already_committed",
        result_id=ex.doc["result_id"] if ex.doc else None,
        descriptor_digest=ex.descriptor_digest,
        evidence_digest=ex.evidence_digest,
        artifacts=ex.entries,
        is_current=True,
        visible=True,
        durable=durable,
    )


def lookup_committed(request: PublicationRequest) -> CommitReceipt | None:
    """Receipt for the committed result this request already names, else None.

    Without `expected_result_id` only the same `operation_id` can be claimed before
    staging; equality of fresh content is judged after measuring, under the lock.
    """
    gate_request(request)
    ex = inspect_destination(request.destination)
    if ex.kind != "committed" or ex.doc is None:
        return None
    same_op = ex.doc["operation_id"] == request.operation_id
    same_result = request.expected_result_id == ex.doc["result_id"]
    if not (same_op or same_result):
        return None
    if request.expected_result_id is not None and not same_result:
        return None
    if not context_matches(request.destination, ex.doc["evidence"]["sha256"], request):
        raise _refuse(request, "conflict", "acceptance_context_differs")
    return _existing_receipt(request, ex, durable=_sync_parent(request.destination))


def _judge_existing(
    prepared: PreparedPublication, ex: ExistingState
) -> CommitReceipt | PublicationRefusedError | None:
    """None means the target is free for the rename."""
    request = prepared.request
    if ex.kind in ("absent", "empty"):
        return None
    if ex.kind == "committed" and ex.doc is not None:
        if ex.doc["destination"] != str(request.destination.resolve()):
            return _refuse(request, "conflict", "destination_mismatch")
        same = (
            ex.doc["result_id"] == prepared.result_id
            and ex.evidence_digest == prepared.evidence_digest
        )
        if same:
            return _existing_receipt(request, ex, durable=_sync_parent(request.destination))
        if ex.doc["operation_id"] == request.operation_id:
            return _refuse(request, "conflict", "operation_conflict")
        if ex.doc["result_id"] == prepared.result_id:
            return _refuse(request, "conflict", "acceptance_context_differs")
        if isinstance(request.overwrite, Replace):
            return _refuse(request, "unsupported", "replace_unsupported_for_unit")
        return _refuse(request, "conflict", "target_occupied_different")
    reasons = {
        "legacy": ("conflict", "target_occupied_legacy"),
        "not_directory": ("conflict", "target_not_directory"),
        "corrupt": ("conflict", f"target_corrupt:{ex.reason}"),
    }
    outcome, reason = reasons[ex.kind]
    return _refuse(request, outcome, reason)  # type: ignore[arg-type]


def commit_directory(prepared: PreparedPublication) -> CommitReceipt:
    """`immutable_directory/v1` commit; raises for every non-success outcome."""
    request, target = prepared.request, prepared.request.destination
    gate_request(request)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = DestinationLock(target)
        lock.__enter__()
    except LockUnavailableError as exc:
        raise _refuse(request, "unsupported", "lock_unavailable") from exc
    try:
        judged = _judge_existing(prepared, inspect_destination(target))
        if isinstance(judged, CommitReceipt):
            return judged
        if judged is not None:
            raise judged
        if request.cancelled is not None and request.cancelled():
            raise _refuse(request, "cancelled_before_commit", "cancelled")
        _reverify_staged_claim(prepared)
        return _rename_and_sync(prepared)
    finally:
        lock.__exit__(None, None, None)


def _reverify_staged_claim(prepared: PreparedPublication) -> None:
    """Under the lock, re-hash payloads, evidence core and descriptor against the claim."""
    request = prepared.request
    try:
        verify_artifacts(prepared.staging, prepared.artifacts)
        for name, digest in (
            (_codec.EVIDENCE_NAME, prepared.evidence_digest),
            (_codec.DESCRIPTOR_NAME, prepared.descriptor_digest),
        ):
            if hash_file(prepared.staging / name)[1] != digest:
                raise MeasureError("claim_changed", name)
    except (MeasureError, OSError) as exc:
        reason = exc.reason_code if isinstance(exc, MeasureError) else "claim_missing"
        raise _refuse(request, "rejected", f"staging_changed:{reason}") from exc


def _rename_and_sync(prepared: PreparedPublication) -> CommitReceipt:
    request, target = prepared.request, prepared.request.destination
    bound: dict[str, Any] = dict(
        result_id=prepared.result_id,
        descriptor_digest=prepared.descriptor_digest,
        evidence_digest=prepared.evidence_digest,
        artifacts=prepared.artifacts,
        is_current=True,
    )
    try:
        os.replace(prepared.staging, target)
    except OSError as exc:
        after = inspect_destination(target)
        if after.kind == "committed" and after.descriptor_digest == prepared.descriptor_digest:
            raise CommitUncertainError(
                "rename reported failure but the binding is visible",
                _receipt(
                    request, "commit_uncertain", "rename_error_visible", visible=True, **bound
                ),
            ) from exc
        raise _refuse(request, "rejected", f"rename_failed:{os_error_token(exc)}") from exc
    try:
        fsync_dir(target.parent)
    except OSError as exc:
        raise CommitUncertainError(
            "published but the destination parent could not be synced",
            _receipt(request, "commit_uncertain", "directory_sync_failed", visible=True, **bound),
        ) from exc
    return _receipt(request, "published", visible=True, durable=True, **bound)


def reconcile(request: PublicationRequest) -> CommitReceipt | None:
    """After `commit_uncertain`: `lookup_committed` under the destination lock, so it can
    never drift from the lookup's identity, context and admission rules."""
    gate_request(request)
    try:
        with DestinationLock(request.destination):
            return lookup_committed(request)
    except LockUnavailableError as exc:
        raise _refuse(request, "unsupported", "lock_unavailable") from exc


def cleanup_staging(path: Path) -> SideStatus:
    if not path.exists():
        return "succeeded"
    shutil.rmtree(path, ignore_errors=True)
    return "failed" if path.exists() else "succeeded"


def with_cleanup(receipt: CommitReceipt, status: SideStatus) -> CommitReceipt:
    return replace(receipt, cleanup_status=status)

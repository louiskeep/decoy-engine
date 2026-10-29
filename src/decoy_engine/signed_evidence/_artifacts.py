"""Artifact binding: open every delivered file safely and hash the bytes actually read.

Pattern: openat-style traversal (``dir_fd`` + ``O_NOFOLLOW`` per component, as in Linux
``openat2(RESOLVE_NO_SYMLINKS)`` and CERT FIO45-C) so a path inside the root cannot be redirected
by a symlink or a swapped directory; the size and the hash both come from the one open descriptor,
never from a separate ``stat`` on the pathname. A FIFO or device is opened non-blocking and refused
on ``fstat`` before any read. Reads stream in bounded chunks.

Extra files under the root are reported as unbound; they cannot be part of a verified release.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ._context import ExpectedArtifact
from ._errors import SignedEvidenceError
from ._result import ArtifactResult, ArtifactStatus
from ._statement import EvidenceStatementV1, check_relative_path

_CHUNK = 1024 * 1024
MAX_TREE_ENTRIES = 200_000
MAX_REPORTED_UNBOUND = 100

_POSIX = os.name == "posix" and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd


@dataclass(frozen=True, slots=True)
class ArtifactEvaluation:
    status: ArtifactStatus
    results: tuple[ArtifactResult, ...]
    unbound: tuple[str, ...]
    reasons: tuple[str, ...]


_PRECEDENCE = (
    ArtifactStatus.MISMATCH,
    ArtifactStatus.MISSING,
    ArtifactStatus.UNAVAILABLE,
    ArtifactStatus.UNEXPECTED,
)


@contextlib.contextmanager
def _open_regular(root_fd: int, rel_path: str) -> Iterator[tuple[int, os.stat_result]]:
    """Open ``rel_path`` under ``root_fd`` without following symlinks; yield ``(fd, fstat)``.

    Raises ``SignedEvidenceError`` (code = reason, status = the artifact status) for a missing, redirected, non-regular or unreadable file. All stat data
    comes from the descriptor that is then read, never from the pathname.
    """
    parts = rel_path.split("/")
    with contextlib.ExitStack() as stack:
        cur = root_fd
        for comp in parts[:-1]:
            try:
                cur = os.open(
                    comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=cur
                )
            except OSError as exc:
                raise _refusal_for(exc, directory=True) from None
            stack.callback(os.close, cur)
        try:
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=cur
            )
        except OSError as exc:
            raise _refusal_for(exc, directory=False) from None
        stack.callback(os.close, fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _refusal(ArtifactStatus.UNAVAILABLE, "ARTIFACT_NOT_REGULAR_FILE")
        yield fd, st


def _refusal(status: ArtifactStatus, reason: str) -> SignedEvidenceError:
    return SignedEvidenceError(reason, status=status.value)


def _refusal_for(exc: OSError, *, directory: bool) -> SignedEvidenceError:
    if exc.errno == errno.ENOENT:
        return _refusal(ArtifactStatus.MISSING, "ARTIFACT_MISSING")
    if exc.errno == errno.ELOOP or (directory and exc.errno == errno.ENOTDIR):
        return _refusal(ArtifactStatus.UNAVAILABLE, "ARTIFACT_PATH_UNSAFE")
    return _refusal(ArtifactStatus.UNAVAILABLE, "ARTIFACT_UNREADABLE")


def _hash_artifact(
    root_fd: int, rel_path: str, size: int, digest: str
) -> tuple[ArtifactStatus, str | None]:
    try:
        with _open_regular(root_fd, rel_path) as (fd, st):
            if st.st_size != size:
                return ArtifactStatus.MISMATCH, "ARTIFACT_LENGTH_MISMATCH"
            h = hashlib.sha256()
            read = 0
            while chunk := os.read(fd, _CHUNK):
                read += len(chunk)
                if read > size:
                    return ArtifactStatus.MISMATCH, "ARTIFACT_LENGTH_MISMATCH"
                h.update(chunk)
            if read != size:
                return ArtifactStatus.MISMATCH, "ARTIFACT_CHANGED_DURING_READ"
            if "sha256:" + h.hexdigest() != digest:
                return ArtifactStatus.MISMATCH, "ARTIFACT_DIGEST_MISMATCH"
            return ArtifactStatus.MATCH, None
    except SignedEvidenceError as refusal:
        return ArtifactStatus(refusal.status), refusal.code


def hash_file_under(
    root: str | os.PathLike[str], rel_path: str, *, make_readonly: bool = False
) -> tuple[int, str]:
    """``(size, "sha256:<hex>")`` of one file under ``root``, read through the safe opener.

    Used when sealing an artifact set before it is described in a statement. The same traversal
    rules apply as when a verifier later reads it, so a file the seal accepts is a file the
    verifier can bind. ``make_readonly`` additionally drops all write bits on the very descriptor
    that was hashed, so sealing cannot be redirected to another file by a swapped path.
    """
    if not _POSIX:
        raise SignedEvidenceError("ARTIFACT_PLATFORM_UNSUPPORTED")
    check_relative_path(rel_path)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise SignedEvidenceError(_refusal_for(exc, directory=True).code) from None
    try:
        with _open_regular(root_fd, rel_path) as (fd, st):
            h = hashlib.sha256()
            read = 0
            while chunk := os.read(fd, _CHUNK):
                read += len(chunk)
                h.update(chunk)
            if read != st.st_size:
                raise SignedEvidenceError("ARTIFACT_CHANGED_DURING_READ")
            if make_readonly:
                os.fchmod(fd, 0o400)
            return read, "sha256:" + h.hexdigest()
    except SignedEvidenceError as refusal:
        raise SignedEvidenceError(refusal.code) from None
    finally:
        os.close(root_fd)


def _unbound_entries(root: Path, bound: set[str]) -> tuple[list[str], bool]:
    """Every non-directory entry (symlinks, FIFOs and devices included) not named by the statement."""
    found: list[str] = []
    seen = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        entries = list(filenames)
        # os.walk lists a symlink-to-directory under dirnames and does not descend into it.
        entries += [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        for name in entries:
            seen += 1
            if seen > MAX_TREE_ENTRIES:
                truncated = True
                return found, truncated
            rel = prefix + name
            if rel not in bound:
                found.append(rel)
    return found, truncated


def bind_artifacts(
    *,
    statement: EvidenceStatementV1,
    expected: tuple[ExpectedArtifact, ...],
    empty_output_allowed: bool,
    root: str | os.PathLike[str] | None,
) -> ArtifactEvaluation:
    reasons: set[str] = set()
    results: list[ArtifactResult] = []
    by_stmt = {a.id: a for a in statement.artifacts}
    by_exp = {a.id: a for a in expected}
    status_seen: set[ArtifactStatus] = set()

    def note(aid: str, status: ArtifactStatus, reason: str | None) -> None:
        results.append(ArtifactResult(aid, status, reason))
        status_seen.add(status)
        if reason:
            reasons.add(reason)

    for aid in sorted(by_exp.keys() - by_stmt.keys()):
        note(aid, ArtifactStatus.MISSING, "ARTIFACT_NOT_IN_STATEMENT")
    for aid in sorted(by_stmt.keys() - by_exp.keys()):
        note(aid, ArtifactStatus.UNEXPECTED, "ARTIFACT_NOT_EXPECTED")
    if not by_exp and not by_stmt and not empty_output_allowed:
        reasons.add("EMPTY_OUTPUT_NOT_ALLOWED")
        status_seen.add(ArtifactStatus.MISSING)

    common = sorted(by_stmt.keys() & by_exp.keys())
    comparable: list[str] = []
    for aid in common:
        s, e = by_stmt[aid], by_exp[aid]
        if s.media_type != e.media_type:
            note(aid, ArtifactStatus.MISMATCH, "ARTIFACT_MEDIA_TYPE_MISMATCH")
        elif s.sha256 != e.sha256:
            note(aid, ArtifactStatus.MISMATCH, "EXPECTED_DIGEST_MISMATCH")
        else:
            comparable.append(aid)

    unbound: list[str] = []
    rejected = statement.publication.state == "REJECTED"
    if rejected:
        for aid in comparable:
            note(aid, ArtifactStatus.WITHHELD, None)
        if root is not None and Path(root).is_dir():
            extras, _ = _unbound_entries(Path(root), set())
            if extras:
                unbound = extras
                reasons.add("ARTIFACT_PRESENT_FOR_REJECTED")
                status_seen.add(ArtifactStatus.UNEXPECTED)
    elif root is None:
        for aid in comparable:
            note(aid, ArtifactStatus.NOT_EVALUATED, "ARTIFACTS_NOT_EVALUATED")
        # Even an empty inventory was not checked against anything on disk.
        status_seen.add(ArtifactStatus.NOT_EVALUATED)
        reasons.add("ARTIFACTS_NOT_EVALUATED")
    elif not _POSIX:
        for aid in comparable:
            note(aid, ArtifactStatus.UNAVAILABLE, "ARTIFACT_PLATFORM_UNSUPPORTED")
    else:
        root_path = Path(root)
        try:
            root_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            missing = exc.errno == errno.ENOENT
            for aid in comparable:
                note(
                    aid,
                    ArtifactStatus.MISSING if missing else ArtifactStatus.UNAVAILABLE,
                    "ARTIFACT_MISSING" if missing else "ARTIFACT_ROOT_UNAVAILABLE",
                )
        else:
            try:
                bound_paths: set[str] = set()
                for aid in comparable:
                    entry = by_stmt[aid]
                    bound_paths.add(entry.path)
                    if entry.sha256 is None or entry.bytes is None:
                        # The schema forbids this for a published artifact; refuse rather than skip.
                        note(aid, ArtifactStatus.UNAVAILABLE, "ARTIFACT_INVENTORY_INVALID")
                        continue
                    st, why = _hash_artifact(root_fd, entry.path, entry.bytes, entry.sha256)
                    note(aid, st, why)
                extras, truncated = _unbound_entries(root_path, bound_paths)
                if extras or truncated:
                    unbound = extras[:MAX_REPORTED_UNBOUND]
                    reasons.add("ARTIFACT_UNBOUND_FILE" if extras else "ARTIFACT_TREE_TOO_LARGE")
                    status_seen.add(ArtifactStatus.UNEXPECTED)
            finally:
                os.close(root_fd)

    for candidate in _PRECEDENCE:
        if candidate in status_seen:
            overall = candidate
            break
    else:
        if status_seen == {ArtifactStatus.NOT_EVALUATED}:
            overall = ArtifactStatus.NOT_EVALUATED
        elif rejected:
            overall = ArtifactStatus.WITHHELD
        else:
            overall = ArtifactStatus.MATCH
    return ArtifactEvaluation(overall, tuple(results), tuple(unbound), tuple(sorted(reasons)))

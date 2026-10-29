"""Private staging, sealing and atomic visibility for signed evidence releases.

Pattern: Spark's ``FileOutputCommitter`` staged-then-renamed publish and the engine's own
``TransactionalSink`` (one ``os.replace`` of a private directory), applied to a whole release:
the artifacts and the signed statement are written into a private ``0700`` staging directory,
the artifact set is sealed (hashed through ``openat``-style descriptors, made read-only), a
statement is signed and a ``ReleasePermit`` is minted by the release evaluator, and only then is
the complete directory exposed with a single rename. Before any commit the artifacts are re-hashed
against the sealed inventory, so a file swapped after sealing never becomes visible.

A crash before the rename leaves nothing under ``releases/``; a crash after it leaves a complete,
signed, verifiable release. The staging directory is invisible to readers by construction (it
lives under a ``0700`` ``.staging`` directory). It cannot defend against code running with the
engine's own privileges; it makes the supported integration paths correct and testable.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from decoy_engine.errors import DecoyError
from decoy_engine.signed_evidence import (
    EVIDENCE_FILENAME,
    SIGNATURE_FILENAME,
    ReleaseEvaluator,
    ReleasePermit,
    ResolvedSigner,
    SignedEvidence,
    SignedEvidenceError,
    VerificationContext,
    check_relative_path,
    hash_file_under,
)
from decoy_engine.signed_evidence import _boundary as _b
from decoy_engine.signed_evidence._release import DenyAllReleaseEvaluator

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TOKEN = object()


class Stage(Enum):
    """Named points a failure can be injected at; mirrors plan A8."""

    STAGED = "staged"
    HASHED = "hashed"
    SIGNED = "signed"
    CHECKED = "checked"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT = "after_commit"


class ReleaseError(DecoyError):
    """A release could not be staged, signed or exposed. The message is the code alone."""

    def __init__(self, code: str, reasons: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.reasons = reasons

    def __str__(self) -> str:
        return self.code


class ReleaseDenied(ReleaseError):  # noqa: N818 -- design-named, mirrors the status it reports
    """The evaluator or a pre-commit check refused to authorize publication."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("RELEASE_DENIED", reasons)


class ReleaseConflict(ReleaseError):  # noqa: N818 -- design-named, mirrors the status it reports
    """A release for this job and attempt is already visible; it is left untouched."""

    def __init__(self) -> None:
        super().__init__("RELEASE_CONFLICT")


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """One declared output: its logical id, safe relative path and media type."""

    id: str
    path: str
    media_type: str


@dataclass(frozen=True, slots=True)
class PublishedRelease:
    path: Path
    signed: SignedEvidence
    permit: ReleasePermit


@dataclass(frozen=True, slots=True)
class RejectedEvidence:
    path: Path
    signed: SignedEvidence


@dataclass(frozen=True, slots=True)
class _Sealed:
    spec: ArtifactSpec
    size: int | None
    digest: str | None


def _write_evidence_files(dest: Path, signed: SignedEvidence) -> None:
    """The only writer of evidence files: exclusive create, fsync, read-only."""
    for name, data in (
        (EVIDENCE_FILENAME, signed.statement),
        (SIGNATURE_FILENAME, signed.signature),
    ):
        fd = os.open(dest / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _publish_dir(src: Path, dst: Path) -> None:
    """Make ``src`` visible as ``dst`` with one rename; an existing target is never replaced."""
    if os.path.lexists(dst):
        raise ReleaseConflict
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR, errno.ENOTDIR):
            raise ReleaseConflict from None
        raise ReleaseError("RELEASE_COMMIT_FAILED") from None
    _fsync_dir(dst.parent)


def _remove_tree(path: Path) -> None:
    """Delete a staged tree even after sealing made it read-only. Never follows symlinks."""
    if not os.path.lexists(path):
        return
    for dirpath, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        for name in filenames:
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(dirpath, name))
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                with contextlib.suppress(OSError):
                    os.unlink(full)
                continue
            with contextlib.suppress(OSError):
                os.chmod(full, 0o700)
                os.rmdir(full)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
        os.rmdir(path)


def _walk_files(root: Path) -> list[str]:
    """Relative POSIX paths of every non-directory entry (symlinks and special files included)."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir == "." else "/".join(Path(rel_dir).parts) + "/"
        found.extend(prefix + n for n in filenames)
        found.extend(prefix + d for d in dirnames if os.path.islink(os.path.join(dirpath, d)))
    return sorted(found)


class StagingCoordinator:
    """Owns the output root. The default evaluator denies every release."""

    def __init__(
        self,
        *,
        root: str | os.PathLike[str],
        signer: ResolvedSigner,
        evaluator: ReleaseEvaluator | None = None,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._root = Path(root)
        self._signer = signer
        self._evaluator: ReleaseEvaluator = evaluator or DenyAllReleaseEvaluator()
        self._fp = failpoint or (lambda _stage: None)

    @property
    def root(self) -> Path:
        return self._root

    def _staging_root(self) -> Path:
        staging = self._root / ".staging"
        self._root.mkdir(parents=True, exist_ok=True)
        if staging.is_symlink():
            raise ReleaseError("STAGING_ROOT_UNSAFE")
        staging.mkdir(mode=0o700, exist_ok=True)
        os.chmod(staging, 0o700)
        return staging

    def begin(self, *, job_id: str, attempt_id: str) -> StagingArea:
        if not (_ID.fullmatch(job_id) and _ID.fullmatch(attempt_id)):
            raise ReleaseError("STAGING_IDENTITY_INVALID")
        path = self._staging_root() / f"{job_id}-{attempt_id}-{secrets.token_hex(6)}"
        path.mkdir(mode=0o700)
        (path / "artifacts").mkdir(mode=0o700)
        return StagingArea(_TOKEN, self, path, job_id, attempt_id)

    def export_verified(
        self,
        dest: str | os.PathLike[str],
        *,
        statement: bytes,
        signature: bytes | None,
        context: VerificationContext,
    ) -> Path:
        """Copy EXISTING evidence after full verification; bytes, signer and key version unchanged."""
        try:
            signed = _b.adopt_verified_evidence(
                statement=statement, signature=signature, context=context
            )
        except SignedEvidenceError:
            raise ReleaseError("EXPORT_NOT_VERIFIED") from None
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".f33-export-{secrets.token_hex(6)}"
        tmp.mkdir(mode=0o700)
        try:
            _write_evidence_files(tmp, signed)
            _publish_dir(tmp, target)
        finally:
            _remove_tree(tmp)
        return target

    def sweep_orphans(self, *, older_than_seconds: float) -> int:
        """Remove staging directories left behind by a crashed process. Returns how many."""
        staging = self._root / ".staging"
        removed = 0
        if not staging.is_dir() or staging.is_symlink():
            return 0
        cutoff = time.time() - older_than_seconds
        for child in staging.iterdir():
            if not child.is_symlink() and child.stat().st_mtime < cutoff:
                _remove_tree(child)
                removed += 1
        return removed


class StagingArea:
    """One attempt's private staging directory. Only ``StagingCoordinator.begin`` creates one."""

    def __init__(
        self,
        token: object,
        coordinator: StagingCoordinator,
        path: Path,
        job_id: str,
        attempt_id: str,
    ) -> None:
        if token is not _TOKEN:
            raise TypeError("StagingArea is created by StagingCoordinator.begin")
        self._coord = coordinator
        self.path = path
        self._job_id = job_id
        self._attempt_id = attempt_id
        self._finished = False

    @property
    def artifacts_dir(self) -> Path:
        return self.path / "artifacts"

    # ----------------------------------------------------------------- staging writes

    def write_artifact(self, rel_path: str, data: bytes) -> None:
        """Write one output file inside the area, refusing anything that could leave it."""
        self._require_open()
        try:
            check_relative_path(rel_path)
        except SignedEvidenceError:
            raise ReleaseError("STAGING_PATH_UNSAFE") from None
        parts = rel_path.split("/")
        with contextlib.ExitStack() as stack:
            cur = os.open(self.artifacts_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            stack.callback(os.close, cur)
            for comp in parts[:-1]:
                try:
                    os.mkdir(comp, 0o700, dir_fd=cur)
                except FileExistsError:
                    pass
                try:
                    cur = os.open(
                        comp,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=cur,
                    )
                except OSError:
                    raise ReleaseError("STAGING_PATH_UNSAFE") from None
                stack.callback(os.close, cur)
            try:
                fd = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=cur,
                )
            except FileExistsError:
                raise ReleaseError("STAGING_ARTIFACT_EXISTS") from None
            except OSError:
                raise ReleaseError("STAGING_PATH_UNSAFE") from None
            stack.callback(os.close, fd)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)

    # ---------------------------------------------------------------------- sealing

    def _seal(self, specs: Sequence[ArtifactSpec], *, allow_missing: bool) -> list[_Sealed]:
        ids = [s.id for s in specs]
        paths = [s.path for s in specs]
        if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
            raise ReleaseError("STAGING_DRAFT_INVALID")
        for p in paths:
            try:
                check_relative_path(p)
            except SignedEvidenceError:
                raise ReleaseError("STAGING_PATH_UNSAFE") from None
        unbound = set(_walk_files(self.artifacts_dir)) - set(paths)
        if unbound:
            raise ReleaseError("STAGING_UNBOUND_FILE")
        sealed: list[_Sealed] = []
        for spec in sorted(specs, key=lambda s: s.id):
            try:
                size, digest = hash_file_under(self.artifacts_dir, spec.path, make_readonly=True)
            except SignedEvidenceError as exc:
                if exc.code == "ARTIFACT_MISSING" and allow_missing:
                    sealed.append(_Sealed(spec, None, None))
                    continue
                code = (
                    "STAGING_ARTIFACT_MISSING"
                    if exc.code == "ARTIFACT_MISSING"
                    else "STAGING_ARTIFACT_UNSAFE"
                )
                raise ReleaseError(code) from None
            sealed.append(_Sealed(spec, size, digest))
        self._seal_directories()
        return sealed

    def _seal_directories(self) -> None:
        """Make every staged directory read-only, bottom-up, through no-follow descriptors."""
        dirs = [
            os.path.join(dp, d)
            for dp, dns, _ in os.walk(self.artifacts_dir, followlinks=False)
            for d in dns
            if not os.path.islink(os.path.join(dp, d))
        ]
        for d in [*sorted(dirs, key=len, reverse=True), str(self.artifacts_dir)]:
            with contextlib.suppress(OSError):
                fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    os.fchmod(fd, 0o500)
                finally:
                    os.close(fd)

    def _still_sealed(self, sealed: Sequence[_Sealed]) -> bool:
        declared = {s.spec.path for s in sealed}
        if set(_walk_files(self.artifacts_dir)) != declared:
            return False
        for s in sealed:
            try:
                size, digest = hash_file_under(self.artifacts_dir, s.spec.path)
            except SignedEvidenceError:
                return False
            if (size, digest) != (s.size, s.digest):
                return False
        return True

    # --------------------------------------------------------------------- assembly

    def _assemble(
        self,
        draft: Mapping[str, Any],
        sealed: Sequence[_Sealed],
        *,
        state: str,
        reason_code: str | None,
    ) -> dict[str, Any]:
        if "artifacts" in draft or "publication" in draft:
            raise ReleaseError("STAGING_DRAFT_INVALID")
        if draft.get("job_id") != self._job_id or draft.get("attempt_id") != self._attempt_id:
            raise ReleaseError("STAGING_DRAFT_INVALID")
        disposition = "INCLUDED" if state == "PUBLISHED" else "WITHHELD"
        artifacts = []
        for s in sealed:
            missing = s.digest is None
            artifacts.append(
                {
                    "id": s.spec.id,
                    "path": s.spec.path,
                    "media_type": s.spec.media_type,
                    "bytes": s.size,
                    "sha256": s.digest,
                    "disposition": "UNAVAILABLE" if missing else disposition,
                }
            )
        out = dict(draft)
        out["artifacts"] = artifacts
        out["publication"] = {"state": state, "reason_code": reason_code}
        return out

    def _sign(self, statement: dict[str, Any]) -> SignedEvidence:
        try:
            return _b.sign_evidence(statement, signer=self._coord._signer)
        except SignedEvidenceError as exc:
            if exc.code.startswith(("STATEMENT_", "CANONICALIZATION")):
                raise ReleaseError("STAGING_DRAFT_INVALID") from None
            raise ReleaseError("SIGNING_UNAVAILABLE") from None

    # ---------------------------------------------------------------------- outcomes

    def publish(
        self,
        draft: Mapping[str, Any],
        specs: Sequence[ArtifactSpec],
        *,
        context: VerificationContext,
        policy_generation: str,
        recheck: Callable[[ReleasePermit], bool] | None = None,
    ) -> PublishedRelease:
        """Seal, sign, authorize and atomically expose one complete release, or expose nothing."""
        self._require_open()
        fp = self._coord._fp
        final = self._coord.root / "releases" / self._job_id / self._attempt_id
        committed = False
        try:
            fp(Stage.STAGED.value)
            sealed = self._seal(specs, allow_missing=False)
            fp(Stage.HASHED.value)
            signed = self._sign(self._assemble(draft, sealed, state="PUBLISHED", reason_code=None))
            fp(Stage.SIGNED.value)
            decision = self._coord._evaluator.evaluate(
                signed,
                artifact_root=self.artifacts_dir,
                context=context,
                policy_generation=policy_generation,
            )
            fp(Stage.CHECKED.value)
            if decision.permit is None:
                raise ReleaseDenied(decision.reasons)
            _write_evidence_files(self.path, signed)
            fp(Stage.BEFORE_COMMIT.value)
            try:
                still_valid = recheck is None or bool(recheck(decision.permit))
            except Exception:  # the recheck is caller code: any failure must deny, never permit
                raise ReleaseDenied(("PERMIT_RECHECK_FAILED",)) from None
            if not still_valid:
                raise ReleaseDenied(("PERMIT_REVOKED",))
            if not self._still_sealed(sealed):
                raise ReleaseDenied(("ARTIFACT_CHANGED_AFTER_SEAL",))
            _publish_dir(self.path, final)
            committed = True
            fp(Stage.AFTER_COMMIT.value)
            return PublishedRelease(final, signed, decision.permit)
        finally:
            self._finished = True
            if not committed:
                _remove_tree(self.path)

    def reject(
        self, draft: Mapping[str, Any], specs: Sequence[ArtifactSpec], *, reason_code: str
    ) -> RejectedEvidence:
        """Sign and export a REJECTED statement with no data; the staged outputs are discarded."""
        self._require_open()
        if not _CODE.fullmatch(reason_code):
            raise ReleaseError("STAGING_DRAFT_INVALID")
        final = self._coord.root / "rejections" / self._job_id / self._attempt_id
        tmp = (
            self._coord._staging_root()
            / f"{self._job_id}-{self._attempt_id}-rej-{secrets.token_hex(6)}"
        )
        try:
            sealed = self._seal(specs, allow_missing=True)
            statement = self._assemble(draft, sealed, state="REJECTED", reason_code=reason_code)
            signed = self._sign(statement)
            tmp.mkdir(mode=0o700)
            _write_evidence_files(tmp, signed)
            _publish_dir(tmp, final)
            return RejectedEvidence(final, signed)
        finally:
            self._finished = True
            _remove_tree(tmp)
            _remove_tree(self.path)

    def discard(self) -> None:
        self._finished = True
        _remove_tree(self.path)

    def _require_open(self) -> None:
        if self._finished:
            raise ReleaseError("STAGING_AREA_FINISHED")

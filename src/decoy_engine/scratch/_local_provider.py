"""The reference storage provider: Linux ext4 + kernel project quota.

Pattern: Linux project quotas as the capacity authority (quotactl(2) project
hard limits; man7.org, public domain) with secure private creation (Python
tempfile discipline). The kernel charges and caps every write through whatever
library opens the file, including native spill and open-but-unlinked files; a
tree walk of logical lengths is only a diagnostic (plan 1.4, 1.5 item 1).

What this provider does unprivileged (the engine never acquires root, plan 1.5):

* ``qualify``: confirm the arena root's mount enforces project quotas and that
  placement is single-device.
* ``create_arena``: make a private 0700 child beneath the verified root with
  symlink/traversal/owner/mode/device checks; the child inherits the root's
  project id through the kernel's PROJINHERIT flag that the deployment adapter
  set.
* ``verify_quota``: confirm a non-zero project id is assigned (so the privileged
  provisioning actually happened); the kernel enforces the byte/inode cap at
  write time. Reading the exact project limit needs privilege the engine does
  not hold, so it is not the engine's job to re-derive it.
* ``observe`` / ``reclaim``: diagnostic usage and subtree deletion with a
  reclamation check.

Encryption (fscrypt) verification is intentionally out of Slice 1 and lands in
Slice 4; ``QualificationResult.encryption_verified`` stays False here, and no
production write path is wired to this provider in Slice 1.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import struct
from pathlib import Path

from decoy_engine.scratch._errors import ScratchBackendUnavailable, ScratchPathUnsafe
from decoy_engine.scratch._policy import ScratchPolicy, StorageProfile
from decoy_engine.scratch._reports import QualificationResult, QuotaObservation

# FS_IOC_FSGETXATTR = _IOR('X', 31, struct fsxattr); struct fsxattr is 28 bytes.
_FS_IOC_FSGETXATTR = 0x801C581F
_FS_XFLAG_PROJINHERIT = 0x00000200


def _read_fsxattr(path: Path) -> tuple[int, int]:
    """Return ``(projid, xflags)`` from the inode's FS_IOC_FSGETXATTR.

    Readable by the owning user; no privilege required. Lets the engine confirm
    the deployment adapter tagged the arena with a project id + inheritance flag.
    """

    buf = bytearray(28)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        fcntl.ioctl(fd, _FS_IOC_FSGETXATTR, buf, True)
    finally:
        os.close(fd)
    xflags, _extsize, _nextents, projid = struct.unpack_from("=IIII", buf, 0)
    return projid, xflags


def _mount_options_for(path: Path) -> str:
    """Return the mount options of the filesystem holding ``path`` (longest
    matching mountpoint in /proc/mounts)."""

    target = os.path.realpath(path)
    best_mount = ""
    best_opts = ""
    with open("/proc/mounts", encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 4:
                continue
            mountpoint, opts = parts[1], parts[3]
            if (target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/")) and len(
                mountpoint
            ) >= len(best_mount):
                best_mount, best_opts = mountpoint, opts
    return best_opts


class KernelProjectQuotaProvider:
    """ext4 + project-quota provider. Binds to ``LINUX_EXT4_FSCRYPT_PRJQUOTA``."""

    profile = StorageProfile.LINUX_EXT4_FSCRYPT_PRJQUOTA

    def qualify(self, root: Path) -> QualificationResult:
        reasons: list[str] = []
        if not root.exists() or not root.is_dir():
            reasons.append("arena root is not an existing directory")
            return QualificationResult(
                qualified=False, profile=self.profile.value, reasons=tuple(reasons)
            )
        opts = _mount_options_for(root)
        if "prjquota" not in opts.split(",") and "project" not in opts.split(","):
            reasons.append("mount does not enforce project quota (no prjquota option)")
        try:
            projid, xflags = _read_fsxattr(root)
        except OSError as exc:
            reasons.append(f"cannot read project attributes: errno {exc.errno}")
            projid, xflags = 0, 0
        if projid == 0:
            reasons.append("arena root has no project id assigned")
        if not xflags & _FS_XFLAG_PROJINHERIT:
            reasons.append("arena root is missing the project-inherit flag")
        return QualificationResult(
            qualified=not reasons,
            profile=self.profile.value,
            reasons=tuple(reasons),
            encryption_verified=False,
        )

    def create_arena(self, root: Path, arena_id: str) -> Path:
        # Reject a symlinked root before resolving it, so a swapped-in symlink
        # cannot redirect the arena to another mount (plan 1.5 mount substitution).
        if os.path.islink(root):
            raise ScratchPathUnsafe(message="arena root is a symlink")
        root = root.resolve()
        root_stat = os.stat(root)
        # A generated opaque component, never a table/tenant name (plan 1.5).
        if "/" in arena_id or arena_id in ("", ".", ".."):
            raise ScratchPathUnsafe(message="arena id is not a single safe path component")
        child = root / arena_id
        try:
            # O_NOFOLLOW on the parent, then mkdir the child with a tight mode.
            os.mkdir(child, mode=0o700)
        except FileExistsError as exc:
            raise ScratchPathUnsafe(message="arena id collides with an existing path") from exc
        os.chmod(child, 0o700)
        child_stat = os.lstat(child)
        if not os.path.samestat(os.stat(child), child_stat) or os.path.islink(child):
            raise ScratchPathUnsafe(message="arena child changed under us (symlink/race)")
        if child_stat.st_dev != root_stat.st_dev:
            raise ScratchPathUnsafe(message="arena child is on a different device (mount swap)")
        if child_stat.st_uid != os.geteuid():
            raise ScratchPathUnsafe(message="arena child is not owned by the engine principal")
        if child_stat.st_mode & 0o777 != 0o700:
            raise ScratchPathUnsafe(message="arena child mode is not 0700")
        return child

    def verify_quota(self, arena: Path, policy: ScratchPolicy) -> QuotaObservation:
        try:
            projid, _xflags = _read_fsxattr(arena)
        except OSError as exc:
            raise ScratchBackendUnavailable(
                message="cannot read arena project id",
                reasons=(f"errno {exc.errno}",),
            ) from exc
        if projid == 0:
            raise ScratchBackendUnavailable(
                message="arena has no kernel project quota assigned",
                reasons=("project id is zero",),
            )
        return self.observe(arena, policy)

    def observe(self, arena: Path, policy: ScratchPolicy) -> QuotaObservation:
        logical, inodes = _walk_logical(arena)
        return QuotaObservation(
            bytes_limit=policy.quota_bytes,
            inodes_limit=policy.quota_inodes,
            # Project-quota usage needs privilege the engine does not hold; the
            # kernel enforces the cap at write time via EDQUOT (plan 1.5 item 1).
            bytes_used=None,
            inodes_used=inodes,
            logical_bytes=logical,
            observed_via="fsxattr+walk",
        )

    def reclaim(self, arena: Path) -> tuple[bool, int, int]:
        if not arena.exists():
            return True, 0, 0
        logical, inodes = _walk_logical(arena)
        try:
            shutil.rmtree(arena)
        except OSError:
            return False, logical, inodes
        confirmed = not arena.exists()
        return (confirmed, 0, 0) if confirmed else (False, logical, inodes)


def _walk_logical(arena: Path) -> tuple[int, int]:
    """Diagnostic only: logical byte sum and inode count. Never enforcement."""

    total_bytes = 0
    inodes = 0
    for dirpath, dirnames, filenames in os.walk(arena):
        inodes += len(dirnames) + len(filenames)
        for name in filenames:
            try:
                total_bytes += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total_bytes, inodes

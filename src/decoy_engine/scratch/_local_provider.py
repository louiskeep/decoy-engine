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

import errno as _errno
import fcntl
import os
import secrets
import stat as _stat
import struct
from pathlib import Path

from decoy_engine.scratch._capabilities import (
    DIR_MODE,
    Identity,
    VerifiedChild,
    VerifiedDir,
)
from decoy_engine.scratch._errors import ScratchBackendUnavailable, ScratchPathUnsafe
from decoy_engine.scratch._policy import ScratchPolicy, StorageProfile
from decoy_engine.scratch._reports import QualificationResult, QuotaObservation

# FS_IOC_FSGETXATTR = _IOR('X', 31, struct fsxattr); struct fsxattr is 28 bytes.
_FS_IOC_FSGETXATTR = 0x801C581F
_FS_XFLAG_PROJINHERIT = 0x00000200

# One ext4 block: the smallest write that charges the byte quota (plan 1.5 item 5).
_PROBE_BLOCK = 4096
_PROBE_EDQUOT = _errno.EDQUOT
_PROBE_ENOSPC = _errno.ENOSPC


# Lifecycle filesystem calls that must execute relative to a verified descriptor.
# os.supports_dir_fd lists os.stat (os.lstat is its follow_symlinks=False variant
# and shares the same dir_fd capability), os.open, os.mkdir, os.unlink, os.rmdir.
# os.fstat / os.fchmod are unconditional fd builtins on POSIX, not members of
# os.supports_fd (which is the optional-fd-as-path set), so they are not checked.
_REQUIRED_DIR_FD = frozenset({os.open, os.mkdir, os.stat, os.unlink, os.rmdir})


def _dir_fd_support_reasons() -> tuple[str, ...]:
    """Reasons the host cannot enforce the fd-relative boundary, or empty when it
    can. The provider targets Linux; a platform without ``dir_fd`` support for
    these lifecycle calls cannot bind operations to a verified descriptor, so it
    fails closed instead of silently degrading to path ops."""

    if not os.supports_dir_fd >= _REQUIRED_DIR_FD:
        return ("os does not support dir_fd for the lifecycle calls",)
    return ()


def _read_fsxattr(fd: int) -> tuple[int, int]:
    """Return ``(projid, xflags)`` from an OPEN inode's FS_IOC_FSGETXATTR.

    Operates on the already-verified ``fd``; it never re-opens a path. Readable by
    the owning user with no privilege, so the engine can confirm the deployment
    adapter tagged the arena with a project id + inheritance flag.
    """

    buf = bytearray(28)
    fcntl.ioctl(fd, _FS_IOC_FSGETXATTR, buf, True)
    xflags, _extsize, _nextents, projid = struct.unpack_from("=IIII", buf, 0)
    return projid, xflags


def _mount_options_for_dev(st_dev: int) -> str:
    """Return the combined per-mount and superblock options of the filesystem
    whose device is ``st_dev``, matched by ``major:minor`` in
    ``/proc/self/mountinfo``. Device identity comes from ``fstat`` on the verified
    fd, so there is no path to re-resolve; ``prjquota`` lives in the superblock
    options (the fields after the ``-`` separator)."""

    want = f"{os.major(st_dev)}:{os.minor(st_dev)}"
    opts = ""
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) < 7 or fields[2] != want:
                continue
            per_mount = fields[5]
            sep = fields.index("-")
            superblock = fields[sep + 3] if len(fields) > sep + 3 else ""
            opts = f"{per_mount},{superblock}"
    return opts


class KernelProjectQuotaProvider:
    """ext4 + project-quota provider. Binds to ``LINUX_EXT4_FSCRYPT_PRJQUOTA``."""

    profile = StorageProfile.LINUX_EXT4_FSCRYPT_PRJQUOTA

    def qualify(self, root: VerifiedDir) -> QualificationResult:
        reasons: list[str] = list(_dir_fd_support_reasons())
        st = os.fstat(root.fd)
        opts = _mount_options_for_dev(st.st_dev)
        if "prjquota" not in opts.split(",") and "project" not in opts.split(","):
            reasons.append("mount does not enforce project quota (no prjquota option)")
        try:
            projid, xflags = _read_fsxattr(root.fd)
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

    def create_arena(self, root: VerifiedDir, arena_id: str) -> tuple[Path, Identity]:
        # A generated opaque component, never a table/tenant name (plan 1.5). The
        # child is created, chmod'd and verified entirely relative to the verified
        # root fd, so a swapped-in symlink at the root cannot redirect it.
        if "/" in arena_id or arena_id in ("", ".", ".."):
            raise ScratchPathUnsafe(message="arena id is not a single safe path component")
        try:
            os.mkdir(arena_id, mode=0o700, dir_fd=root.fd)
        except FileExistsError as exc:
            raise ScratchPathUnsafe(message="arena id collides with an existing path") from exc
        try:
            child_fd = os.open(
                arena_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=root.fd
            )
        except OSError:
            raise ScratchPathUnsafe(message="arena child changed under us (symlink/race)") from None
        try:
            os.fchmod(child_fd, 0o700)
            cst = os.fstat(child_fd)
        finally:
            os.close(child_fd)
        if cst.st_dev != root.identity[0]:
            raise ScratchPathUnsafe(message="arena child is on a different device (mount swap)")
        if cst.st_uid != os.geteuid():
            raise ScratchPathUnsafe(message="arena child is not owned by the engine principal")
        if cst.st_mode != DIR_MODE:
            raise ScratchPathUnsafe(message="arena child mode is not 0700")
        return root.path / arena_id, (cst.st_dev, cst.st_ino)

    def verify_quota(self, arena: VerifiedDir, policy: ScratchPolicy) -> QuotaObservation:
        try:
            projid, _xflags = _read_fsxattr(arena.fd)
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

    def observe(self, arena: VerifiedDir, policy: ScratchPolicy) -> QuotaObservation:
        logical, inodes = _walk_logical_fd(arena.fd)
        return QuotaObservation(
            bytes_limit=policy.quota_bytes,
            inodes_limit=policy.quota_inodes,
            # Project-quota usage needs privilege the engine does not hold; the
            # kernel enforces the cap at write time via EDQUOT (plan 1.5 item 1).
            bytes_used=None,
            inodes_used=inodes,
            logical_bytes=logical,
            observed_via="fsxattr+fwalk",
        )

    def probe_exhaustion(self, arena: VerifiedDir, policy: ScratchPolicy) -> tuple[bool, str]:
        # One inode + one filesystem block, created then immediately removed,
        # entirely relative to the verified arena fd. The kernel is the authority:
        # EDQUOT/ENOSPC on create is inode pressure, on the block write it is byte
        # pressure (plan 1.5 item 5). Any other errno (EACCES, a vanished arena) is
        # not quota evidence, so the caller falls to the unknown-I/O classification.
        # Unpredictable exclusive name (not a PID, which can collide): O_EXCL fails
        # closed on any clash.
        probe = f".decoy-quota-probe-{secrets.token_hex(16)}"
        fd = -1
        try:
            fd = os.open(
                probe,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=arena.fd,
            )
        except OSError as exc:
            if exc.errno in (_PROBE_EDQUOT, _PROBE_ENOSPC):
                return True, "inodes"
            return False, ""
        try:
            os.write(fd, b"\0" * _PROBE_BLOCK)
            os.fsync(fd)
        except OSError as exc:
            if exc.errno in (_PROBE_EDQUOT, _PROBE_ENOSPC):
                return True, "bytes"
            return False, ""
        finally:
            os.close(fd)
            try:
                os.unlink(probe, dir_fd=arena.fd)
            except OSError:
                pass
        return False, ""

    def reclaim(self, target: VerifiedChild) -> tuple[bool, int, int]:
        """Delete the verified target, bound to the pinned parent fd. The parent fd
        is held open by the caller's ``open_verified_target`` context, so there is
        no path re-resolution between the identity check and the delete."""

        expect_dir = _stat.S_ISDIR(target.expected_mode)
        if expect_dir:
            try:
                child_fd = os.open(
                    target.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                    dir_fd=target.parent_fd,
                )
            except FileNotFoundError:
                return True, 0, 0
            except OSError:
                raise ScratchPathUnsafe(
                    message="reclaim child could not be opened no-follow"
                ) from None
            try:
                cst = os.fstat(child_fd)
                if (cst.st_dev, cst.st_ino) != target.identity:
                    raise ScratchPathUnsafe(message="reclaim child identity changed")
                logical, inodes = _delete_tree_fd(child_fd)
            finally:
                os.close(child_fd)
            remove = os.rmdir
        else:
            try:
                logical, inodes = os.lstat(target.name, dir_fd=target.parent_fd).st_size, 1
            except FileNotFoundError:
                return True, 0, 0
            remove = os.unlink
        try:
            remove(target.name, dir_fd=target.parent_fd)
        except FileNotFoundError:
            pass
        except OSError:
            return False, logical, inodes
        if _entry_absent(target.parent_fd, target.name):
            return True, 0, 0
        return False, logical, inodes


def _entry_absent(dir_fd: int, name: str) -> bool:
    """True iff ``name`` no longer resolves under the pinned parent fd. Any
    ``OSError`` other than ``FileNotFoundError`` leaves the fate unconfirmed, so
    the entry is treated as still present."""

    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _walk_logical_fd(dir_fd: int) -> tuple[int, int]:
    """Diagnostic only: logical byte sum and inode count, walked FD-relative with
    ``os.fwalk`` (never ``os.walk`` on a path string). Never enforcement."""

    total_bytes = 0
    inodes = 0
    for _dirpath, dirnames, filenames, walk_fd in os.fwalk(dir_fd=dir_fd, follow_symlinks=False):
        inodes += len(dirnames) + len(filenames)
        for name in filenames:
            try:
                total_bytes += os.lstat(name, dir_fd=walk_fd).st_size
            except OSError:
                continue
    return total_bytes, inodes


def _delete_tree_fd(dir_fd: int) -> tuple[int, int]:
    """Delete every child of an open directory fd, FD-relative and no-follow,
    refusing to cross a device boundary. Returns the ``(logical_bytes, inodes)``
    removed, so a failed removal still reports a diagnostic charge. Never follows
    a symlink and never re-resolves a path string."""

    dev = os.fstat(dir_fd).st_dev
    total_bytes = 0
    inodes = 0
    for child in os.listdir(dir_fd):
        try:
            st = os.lstat(child, dir_fd=dir_fd)
        except OSError:
            continue
        if st.st_dev != dev:
            continue
        inodes += 1
        if _stat.S_ISDIR(st.st_mode):
            try:
                sub = os.open(child, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=dir_fd)
            except OSError:
                continue
            try:
                sub_bytes, sub_inodes = _delete_tree_fd(sub)
            finally:
                os.close(sub)
            total_bytes += sub_bytes
            inodes += sub_inodes
            try:
                os.rmdir(child, dir_fd=dir_fd)
            except OSError:
                continue
        else:
            total_bytes += st.st_size
            try:
                os.unlink(child, dir_fd=dir_fd)
            except OSError:
                continue
    return total_bytes, inodes

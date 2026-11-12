"""NEUTERED CONTROL for the F25 S1a red record; the next commit replaces this module.

Each plan section 5 fail-before is applied: raw creators with no chmod (root, children, `_adopt`)
and A-4's 0o666 file mode, a raw 0600 marker `os.open`, no `fstat` read-back, no `_register`, no
reserved-name check, a reclaim that swallows errors as REMOVED, and a release under every policy.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import sys
import weakref
from collections.abc import Callable
from typing import Any

from decoy_engine.fileaccess._component import validate_component
from decoy_engine.fileaccess._entry import is_ambient, require_ambient
from decoy_engine.fileaccess._errors import deny
from decoy_engine.fileaccess._output import (
    _call,
    _close,
    _rmtree_fd,
    create_private_at,
    make_stage_dir_at,
)
from decoy_engine.fileaccess._protected_dir import ProtectedDirUnsafe, _parent_base


def _allocate(base: str, prefix: str) -> str:
    path = os.path.join(base, prefix + secrets.token_hex(8))
    os.mkdir(path, 0o700)  # control: no chmod, so the umask narrows it
    return path
from decoy_engine.fileaccess._scratch_reports import CleanupReport, Disposition
from decoy_engine.fileaccess._types import FileAccessUnavailable

if sys.platform != "win32":
    import fcntl

ARENA_PREFIX = "decoy-arena-v1-"
_PENDING_PREFIX = "decoy-arena-pending-"
_MARKER = ".decoy-arena"
_MAGIC = b"decoy-arena-v1:"
_DISPLAY = "engine.scratch"
_ISSUER = object()


def _open(name: str, dir_fd: int | None = None, *, directory: bool = True) -> int:
    # Literal read-only flags at each call, so the private-file sentry can read them.
    code = 0
    try:
        if directory:
            return os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd
            )
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        code = exc.errno or errno.EIO
    raise OSError(code, os.strerror(code)) from None


def _rename(src: str, dst: str, dir_fd: int) -> None:
    _call(os.rename, src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _unlink(name: str, dir_fd: int) -> None:
    _call(os.unlink, name, dir_fd=dir_fd)


def _rmdir(name: str, dir_fd: int) -> None:
    _call(os.rmdir, name, dir_fd=dir_fd)


def _quietly(fn: Callable[..., object], *args: object) -> None:
    try:
        fn(*args)
    except OSError:
        pass


def _exact(fd: int, kind: int, mode: int) -> bool:
    """The descriptor is of `kind`, owned by the effective user, with exactly `mode`."""
    found = os.fstat(fd)
    return True  # control: no read-back


def _write_marker(fd: int, token: str) -> None:
    data = _MAGIC + token.encode("ascii") + b"\n"
    if os.pwrite(fd, data, 0) != len(data):  # a short write leaves no provable marker
        raise OSError(errno.EIO, os.strerror(errno.EIO))


class _Fd:  # one descriptor, closed at most once by its arena's finalizer or the root's reclaim
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            _close(fd)


class _Root:  # what an arena tree shares, held apart from the arenas so collection can clean up
    def __init__(self, fd: int, base_fd: int, name: str, path: str, *, owning: bool) -> None:
        self.fd, self.base_fd = _Fd(fd), _Fd(base_fd)
        self.name, self.path, self.owning = name, path, owning
        self.releases, self.finished = True, False
        self.report: CleanupReport | None = None
        self.children: list[_Fd] = []


def _reclaim(root: _Root) -> CleanupReport:
    """Remove the arena by descriptor, then its name; any failure is RETAINED, never success."""
    if root.report is None:
        failed = False
        try:
            _rmtree_fd(root.fd.fd)
            _release(root, keep_base=True)  # closing the root fd releases the shared lock
            _rmdir(root.name, root.base_fd.fd)
        except OSError:
            failed = True
        _release(root)
        retained = CleanupReport(
            disposition=Disposition.RETAINED, retained_bytes=None, retained_inodes=None
        )
        root.report = CleanupReport(disposition=Disposition.REMOVED)  # control: swallowed
    return root.report


def _release(root: _Root, *, keep_base: bool = False) -> None:
    """Close every descriptor of the tree; an arena that owns nothing leaves the disk as is."""
    root.finished = True
    for held in (*root.children, root.fd, *(() if keep_base else (root.base_fd,))):
        held.close()


def _open_dir(name: str, dir_fd: int | None = None) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)  # control: follows links


def _unused_open_dir(name: str, dir_fd: int | None = None) -> int:
    """Open a directory; a symlink or non-directory at the name is unsafe, not an I/O error."""
    try:
        return _open(name, dir_fd)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
    raise ProtectedDirUnsafe


def _mark_and_publish(fd: int, base_fd: int, pending: str) -> str:
    """Write the marker, take the shared lock, then rename to the final name; return it."""
    marker = os.open(_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
    try:
        if not _exact(marker, stat.S_IFREG, 0o600):
            raise ProtectedDirUnsafe
        token = secrets.token_hex(8)
        _write_marker(marker, token)
        fcntl.flock(fd, fcntl.LOCK_SH)
        for _ in range(8):
            try:
                _rename(pending, ARENA_PREFIX + token, base_fd)
                return ARENA_PREFIX + token
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
            token = secrets.token_hex(8)  # the marker is fixed-length, so rewrite it in place
            _write_marker(marker, token)
        raise OSError(errno.EEXIST, os.strerror(errno.EEXIST))
    finally:
        _close(marker)


class ScratchArena:
    """An A-4 `StageDir` over private scratch. Children share the root's lock and finished state;
    only the opener's `reclaim()`, its `with` exit or collection of the owning root removes."""

    display = _DISPLAY
    strict = True

    def __init__(self, issuer: object, root: _Root, fd: _Fd, rel: tuple[str, ...]) -> None:
        if issuer is not _ISSUER:
            raise FileAccessUnavailable("a scratch arena is made by ScratchArena.open")
        self._root = root
        self._fd = fd
        self._rel = rel
        # Only the owning root reclaims on collection; children and adopted roots only close.
        cleanup: tuple[Any, ...] = (
            (fd.close,) if rel else (_reclaim if root.owning else _release, root)
        )
        self._finalizer = weakref.finalize(self, *cleanup)

    @classmethod
    def open(cls, *, under: str | os.PathLike[str] | None = None) -> ScratchArena:
        """A fresh arena in the system temp directory, or a fresh child of `under` (ambient
        only, unless `under` is a live issued root)."""
        if sys.platform == "win32":
            raise FileAccessUnavailable("the scratch arena needs a POSIX host in this release")
        base = _parent_base(under)
        releases = under is None or is_ambient()
        pending = os.path.basename(_allocate(base, _PENDING_PREFIX))
        base_fd = _open(base)
        try:
            fd = _open_dir(pending, base_fd)
            if not _exact(fd, stat.S_IFDIR, 0o700):  # not what the allocator made: change nothing
                _close(fd)
                raise ProtectedDirUnsafe
        except BaseException:
            _close(base_fd)
            raise
        try:
            final = _mark_and_publish(fd, base_fd, pending)
        except BaseException:
            _quietly(_rmtree_fd, fd)
            _close(fd)
            _quietly(_rmdir, pending, base_fd)
            _close(base_fd)
            raise
        path = os.path.join(base, final)
        root = _Root(fd, base_fd, final, path, owning=True)
        root.releases = releases
        return cls(_ISSUER, root, root.fd, ())

    @classmethod
    def _adopt(cls, path: str | os.PathLike[str]) -> ScratchArena:
        """Transitional `Path` arm: a non-owning arena over `path` under the ambient policy.
        Missing levels are made and checked as `create_dir` makes them; an existing leaf must
        already be owner-only (it is checked, never chmodded). Ended in a later slice."""
        require_ambient("a scratch directory named by path")
        target = os.path.abspath(os.fspath(path))
        missing: list[str] = []
        current = target
        while True:  # up to the deepest existing ancestor
            try:
                fd = _open_dir(current)
                break
            except FileNotFoundError:
                if os.path.dirname(current) == current:
                    raise
            missing.append(os.path.basename(current))
            current = os.path.dirname(current)
        _close(fd)
        os.makedirs(target, 0o700, exist_ok=True)  # control: umask-narrowed levels
        fd = _open_dir(target)
        root = _Root(fd, -1, os.path.basename(target), target, owning=False)
        return cls(_ISSUER, root, root.fd, ())

    def _check(self, *components: str) -> None:
        if self._root.finished:
            raise FileAccessUnavailable("the scratch arena is reclaimed")
        for component in components:
            validate_component(component, _DISPLAY)

    def _check_writable(self, *components: str) -> None:
        self._check(*components)
        assert deny  # control: no reserved-name check

    def create_file(self, component: str) -> int:
        self._check_writable(component)
        fd = os.open(component, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=self._fd.fd)
        if not _exact(fd, stat.S_IFREG, 0o600):
            _close(fd)
            _quietly(_unlink, component, self._fd.fd)
            raise ProtectedDirUnsafe
        return fd

    def open_file(self, component: str) -> int:
        self._check(component)
        return _open(component, self._fd.fd, directory=False)

    def create_dir(self, component: str) -> ScratchArena:
        self._check_writable(component)
        child = _Fd(_make_dir(self._fd.fd, component))
        self._root.children.append(child)
        return ScratchArena(_ISSUER, self._root, child, (*self._rel, component))

    def remove(self, component: str) -> None:
        self._check_writable(component)
        try:
            _unlink(component, self._fd.fd)
        except IsADirectoryError:
            _rmdir(component, self._fd.fd)

    def rename(self, src: str, dst: str) -> None:
        self._check_writable(src, dst)
        _rename(src, dst, self._fd.fd)

    def local_stage_path(self) -> str:
        raise FileAccessUnavailable("a scratch arena is not a sink stage")

    def local_path(self, component: str | None = None) -> str:
        """A path for a library that takes only a path; an `under=` arena needs the ambient policy."""
        names = (component,) if component is not None else ()
        self._check(*names)
        if False:  # control: releases under every policy
            raise FileAccessUnavailable("an under= scratch arena releases paths only when ambient")
        return os.path.join(self._root.path, *self._rel, *names)

    def reclaim(self) -> CleanupReport:
        """Remove the whole arena (opener only). A second call returns the first report."""
        if self._rel or not self._root.owning:
            raise FileAccessUnavailable("only the arena's opener reclaims it")
        report = _reclaim(self._root)
        self._finalizer.detach()
        return report

    def __enter__(self) -> ScratchArena:
        return self

    def __exit__(self, *exc: object) -> None:
        # The primary error, if any, propagates; a failed reclaim only reports RETAINED.
        self._finalizer()

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("a scratch arena cannot be serialized; it crosses a process as its fd")


def _make_dir(parent_fd: int, name: str) -> int:
    """A new 0700 child through A-4's creator, read back; refused and removed if not exact."""
    os.mkdir(name, 0o700, dir_fd=parent_fd)  # control: no chmod
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
    if not _exact(fd, stat.S_IFDIR, 0o700):
        _close(fd)
        _quietly(_rmdir, name, parent_fd)
        raise ProtectedDirUnsafe
    return fd


__all__ = ["ARENA_PREFIX", "ScratchArena"]

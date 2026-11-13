"""Engine scratch as one A-4 capability: a held, owner-only directory, private under any umask.

Pattern: `cap-std` `Dir` and WASI preopens (operations relative to a held directory descriptor);
`flock(2)` on the root's open file description, the liveness lock `systemd-tmpfiles(8)` honours;
Maildir's pending-then-rename (`tmp/` then `new/`), with a marker inside proving the creator.

The arena creates nothing itself: roots and missing `under=` levels come from L0.1's allocator,
children and private files from A-4's named creators in `_output.py`. Each new descriptor is read
back with `fstat`; anything but an owner-only 0700 directory or 0600 file is refused and removed.
Creator errno failures pass through unchanged. `local_path` is the one path release. Named limits:
a crash inside `open` leaves an empty pending directory (L4); same-user interference (L6).
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
import sys
import weakref
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from typing import Any, TypeVar

from decoy_engine.fileaccess._component import validate_component
from decoy_engine.fileaccess._entry import is_ambient, require_ambient
from decoy_engine.fileaccess._errors import deny, guarded, guarded_iter
from decoy_engine.fileaccess._output import (
    _call,
    _close,
    _rmtree_fd,
    create_private_at,
    make_stage_dir_at,
)
from decoy_engine.fileaccess._protected_dir import (
    ProtectedDirUnsafe,
    _allocate,
    _owned_private,
    _parent_base,
    _register,
    _unregister,
)
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
T = TypeVar("T")


def _open(name: str, dir_fd: int | None = None, *, directory: bool = True) -> int:
    # Literal read-only flags at each call, so the private-file sentry can read them.
    if directory:
        return _call(
            os.open,
            name,
            flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=dir_fd,
        )
    return _call(os.open, name, flags=os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)


def _open_base(path: str) -> int:
    # The policy already vetted `path`, and a temp directory may be a link (macOS /tmp).
    return _call(os.open, path, flags=os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _rename(src: str, dst: str, dir_fd: int) -> None:
    _call(os.rename, src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _unlink(name: str, dir_fd: int) -> None:
    _call(os.unlink, name, dir_fd=dir_fd)


def _rmdir(name: str, dir_fd: int) -> None:
    _call(os.rmdir, name, dir_fd=dir_fd)


def _quietly(fn: Callable[..., object], *args: object) -> None:
    with suppress(OSError):
        fn(*args)


def _exact(fd: int, kind: int, mode: int) -> bool:
    """The descriptor is of `kind`, owned by the effective user, with exactly `mode`."""
    found = os.fstat(fd)
    same = stat.S_IFMT(found.st_mode) == kind and stat.S_IMODE(found.st_mode) == mode
    return same and _owned_private(found)


def _write_marker(fd: int, token: str) -> None:
    if os.pwrite(fd, data := _MAGIC + token.encode("ascii") + b"\n", 0) != len(data):
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
        _unregister(root.path)
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
        root.report = retained if failed else CleanupReport(disposition=Disposition.REMOVED)
    return root.report


def _release(root: _Root, *, keep_base: bool = False) -> None:
    """Close every descriptor of the tree; an arena that owns nothing leaves the disk as is."""
    root.finished = True
    for held in (*root.children, root.fd, *(() if keep_base else (root.base_fd,))):
        held.close()


def _open_dir(name: str, dir_fd: int | None = None) -> int:
    """Open a directory; a symlink or non-directory at the name is unsafe, not an I/O error."""
    try:
        return _open(name, dir_fd)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
    raise ProtectedDirUnsafe


def _mark_and_publish(fd: int, base_fd: int, pending: str) -> str:
    """Write the marker, take the shared lock, then rename to the final name; return it."""
    marker = create_private_at(fd, _MARKER)
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

    def __init__(
        self,
        issuer: object,
        root: _Root,
        fd: _Fd,
        rel: tuple[str, ...],
        opener: ScratchArena | None = None,
    ) -> None:
        if issuer is not _ISSUER:
            raise FileAccessUnavailable("a scratch arena is made by ScratchArena.open")
        self._root = root
        self._fd = fd
        self._rel = rel
        # The opener's finalizer owns the whole tree, so a live child keeps it from collection.
        self._opener = opener
        # Only the owning root reclaims on collection; children and adopted roots only close.
        cleanup: tuple[Any, ...] = (
            (fd.close,) if rel else (_reclaim if root.owning else _release, root)
        )
        self._finalizer = weakref.finalize(self, *cleanup)

    @classmethod
    def open(cls, *, under: str | os.PathLike[str] | None = None) -> ScratchArena:
        """A fresh arena in the temp directory, or under `under` (ambient or a live issued root)."""
        if sys.platform == "win32":
            raise FileAccessUnavailable("the scratch arena needs a POSIX host in this release")
        base = _parent_base(under)
        base_fd = _open_base(base)
        fd, pending, ours = -1, "", False
        try:
            pending = os.path.basename(_allocate(base, _PENDING_PREFIX))
            fd = _open_dir(pending, base_fd)
            # Not what the allocator made: change nothing on disk.
            if not _exact(fd, stat.S_IFDIR, 0o700):
                raise ProtectedDirUnsafe
            ours = True
            final = _mark_and_publish(fd, base_fd, pending)
        except BaseException:
            if ours:
                _quietly(_rmtree_fd, fd)
                _quietly(_rmdir, pending, base_fd)
            if fd >= 0:
                _close(fd)
            _close(base_fd)
            raise
        root = _Root(fd, base_fd, final, os.path.join(base, final), owning=True)
        _register(root.path)
        root.releases = under is None or is_ambient()  # the release rule is fixed at open
        return cls(_ISSUER, root, root.fd, ())

    @classmethod
    def _adopt(cls, path: str | os.PathLike[str]) -> ScratchArena:
        """Transitional non-owning `Path` arm (ambient only): missing levels made as `create_dir`
        makes them; an existing leaf must already be owner-only, checked and never chmodded."""
        require_ambient("a scratch directory named by path")
        target = os.path.abspath(os.fspath(path))
        missing, current = [], target
        while True:  # up to the deepest existing ancestor
            try:
                fd = _open_dir(current)
                break
            except FileNotFoundError:
                if os.path.dirname(current) == current:
                    raise
            missing.append(os.path.basename(current))
            current = os.path.dirname(current)
        if not missing and not _exact(fd, stat.S_IFDIR, 0o700):
            _close(fd)
            raise ProtectedDirUnsafe
        for name in reversed(missing):
            try:
                child = _make_dir(fd, name)
            finally:
                _close(fd)
            fd = child
        adopted = _Root(fd, -1, os.path.basename(target), target, owning=False)
        return cls(_ISSUER, adopted, adopted.fd, ())

    def _check(self, *components: str, writes: bool = False) -> None:
        if self._root.finished:
            raise FileAccessUnavailable("the scratch arena is reclaimed")
        for component in components:
            validate_component(component, _DISPLAY)
        if writes and _MARKER in components:
            raise deny(_DISPLAY)  # the marker is the sweep's proof of ownership

    def create_file(self, component: str) -> int:
        self._check(component, writes=True)
        fd = create_private_at(self._fd.fd, component)
        if not _exact(fd, stat.S_IFREG, 0o600):
            _close(fd)
            _quietly(_unlink, component, self._fd.fd)
            raise ProtectedDirUnsafe
        return fd

    def open_file(self, component: str) -> int:
        self._check(component)
        return _open(component, self._fd.fd, directory=False)

    def create_dir(self, component: str) -> ScratchArena:
        self._check(component, writes=True)
        child = _Fd(_make_dir(self._fd.fd, component))
        self._root.children.append(child)
        opener = self._opener or self
        return ScratchArena(_ISSUER, self._root, child, (*self._rel, component), opener)

    def remove(self, component: str) -> None:
        self._check(component, writes=True)
        try:
            _unlink(component, self._fd.fd)
            return
        except (IsADirectoryError, PermissionError) as exc:
            failed = exc  # macOS refuses to unlink a directory with EPERM, Linux with EISDIR
        directory = True
        try:
            _close(_open(component, self._fd.fd))  # opens a real directory, never a link to one
        except OSError:
            directory = False
        if not directory:
            raise failed
        _rmdir(component, self._fd.fd)

    def rename(self, src: str, dst: str) -> None:
        self._check(src, dst, writes=True)
        _rename(src, dst, self._fd.fd)

    def local_stage_path(self) -> str:
        raise FileAccessUnavailable("a scratch arena is not a sink stage")

    def local_path(self, component: str | None = None) -> str:
        """A path for a library that takes only a path; an `under=` arena needs the ambient policy."""
        names = (component,) if component is not None else ()
        self._check(*names)
        if not self._root.releases:
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
    fd = make_stage_dir_at(parent_fd, name)
    if not _exact(fd, stat.S_IFDIR, 0o700):
        _close(fd)
        _quietly(_rmdir, name, parent_fd)
        raise ProtectedDirUnsafe
    return fd


def scratch_native(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run one native call (DuckDB, pyarrow) on scratch, under every policy, value-free.

    The mapping is `_errors.guarded`'s, strict whatever the policy, by type, never by text:
    memory failures keep their class (`passes_by_type`, or rebuilt over scrubbed text by
    `resurface_scrubbed`, so `is_memory_failure` still matches), a resource-errno `OSError`
    keeps its class rebuilt errno-only, a denial passes, and anything else becomes
    `FileAccessReadError("engine.scratch")`. Each new error is raised after the `except` block,
    so neither `__context__` nor `__cause__` holds the library's text.
    """
    return guarded(_DISPLAY, True, fn, *args, **kwargs)


def scratch_native_iter(batches: Iterable[T]) -> Iterator[T]:
    """Each pull of a native batch iterator under `scratch_native`'s mapping."""
    return guarded_iter(_DISPLAY, True, batches)


__all__ = ["ARENA_PREFIX", "ScratchArena", "scratch_native", "scratch_native_iter"]

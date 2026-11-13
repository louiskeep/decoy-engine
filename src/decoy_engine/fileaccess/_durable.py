"""Durable binding over held descriptors: the fsync walk, the destination lock and bounded
member reads, shared by both POSIX policies (F22 5.0 rule D, 5.1).

Pattern: PostgreSQL's `fsync_fname` and `durable_rename`, and SQLite's atomic commit: fsync every
file and directory of what is bound, then the directory the rename changed, then each directory
above it, before calling it durable; state found on disk is fsynced before it is trusted, as
`SyncDataDirectory` does at startup. Every descriptor is `O_RDONLY` (Linux fsyncs it), so a unit
of 0400 files and 0500 directories can still be made durable. The lock is an advisory
`flock(2)` on a file beside the leaf, as SQLite's unix VFS and `filelock` use it, with no
lock-free fallback. Failures keep only their errno.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import time
from collections.abc import Callable, Iterator

from decoy_engine.fileaccess._component import LOCK_PREFIX, LOCK_SUFFIX
from decoy_engine.fileaccess._output import (
    _DIR_FLAGS,
    _call,
    _close,
    _restore_missing_owner_bits,
)
from decoy_engine.fileaccess._types import (
    CommitUncertain,
    LeafTooLarge,
    LockTimeout,
    LockUnavailable,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock; the lock is refused there
    fcntl = None  # type: ignore[assignment]

_POLL_S = 0.05
_CHUNK = 1 << 16


def _leaf_flags() -> int:
    # O_NONBLOCK: a FIFO planted in a unit must not block the open.
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _sync_tree(dir_fd: int) -> None:
    """fsync every regular file and directory beneath `dir_fd`, deepest first, then `dir_fd`
    itself; no link is followed."""
    for entry in list(os.scandir(dir_fd)):
        if entry.is_dir(follow_symlinks=False):
            child_fd = _call(os.open, entry.name, _DIR_FLAGS, dir_fd=dir_fd)
            try:
                _sync_tree(child_fd)
            finally:
                _close(child_fd)
        elif entry.is_file(follow_symlinks=False):
            child_fd = _call(os.open, entry.name, _leaf_flags(), dir_fd=dir_fd)
            try:
                _call(os.fsync, child_fd)
            finally:
                _close(child_fd)
    _call(os.fsync, dir_fd)


def _sync_leaf_at(parent_fd: int, leaf: str) -> None:
    """fsync a bound leaf: its whole tree when it is a directory, else the file."""
    leaf_fd = _call(os.open, leaf, _leaf_flags(), dir_fd=parent_fd)
    try:
        if stat.S_ISDIR(os.fstat(leaf_fd).st_mode):
            _sync_tree(leaf_fd)
        else:
            _call(os.fsync, leaf_fd)
    finally:
        _close(leaf_fd)


def _sync_fds(chain_fds: list[int]) -> None:
    """fsync held directories in the order given (deepest first)."""
    for dir_fd in chain_fds:
        _call(os.fsync, dir_fd)


def _sync_bound(sync: Callable[[], None]) -> None:
    """Run the fsyncs after a bind: the leaf is already bound, so a failure is
    `CommitUncertain` (raised outside the handler so no OS text rides along)."""
    code = 0
    try:
        sync()
    except OSError as exc:
        code = exc.errno or errno.EIO
    if code:
        raise CommitUncertain(code)


@contextlib.contextmanager
def _held_lock(open_parent: Callable[[], int], leaf: str, timeout_s: float) -> Iterator[None]:
    """`flock(LOCK_EX)` on `.<leaf>.decoy-lock` in the leaf's parent, polled to a deadline.
    Closing the one descriptor releases it (the kernel does so on any exit, SIGKILL included);
    the file is never unlinked, since unlinking a lock file lets two holders lock two inodes."""
    if fcntl is None:
        raise LockUnavailable(errno.ENOSYS)
    code = 0
    lock_fd = -1
    try:
        parent_fd = open_parent()
        try:
            lock_fd = _open_lock_at(parent_fd, leaf)
        finally:
            _close(parent_fd)
    except OSError as exc:
        code = exc.errno or errno.EIO
    if code:
        raise LockUnavailable(code)
    try:
        _wait_for_lock(lock_fd, timeout_s)
        yield
    finally:
        _close(lock_fd)


def _open_lock_at(parent_fd: int, leaf: str) -> int:
    # O_RDWR because NFS emulates flock with byte-range locks, which need write access.
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    lock_fd = _call(os.open, f"{LOCK_PREFIX}{leaf}{LOCK_SUFFIX}", flags, 0o600, dir_fd=parent_fd)
    try:
        _restore_missing_owner_bits(lock_fd)  # under umask 0477 it is born 0200
    except OSError:
        _close(lock_fd)
        raise
    return lock_fd


def _wait_for_lock(lock_fd: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        code = 0
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        except OSError as exc:  # ENOLCK, EOPNOTSUPP, EINVAL, EBADF: no flock here
            code = exc.errno or errno.EIO
        if code:
            raise LockUnavailable(code)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LockTimeout(errno.EWOULDBLOCK)
        time.sleep(min(_POLL_S, remaining))


def _read_member(
    open_parent: Callable[[], int], leaf: str, component: str, limit: int
) -> bytes | None:
    """At most `limit` bytes of regular file `component` inside directory `leaf`; `None` when
    the parent, the leaf or the member is absent; over the limit raises `LeafTooLarge`."""
    parent_fd = -1
    try:
        parent_fd = open_parent()
        return _read_member_at(parent_fd, leaf, component, limit)
    except FileNotFoundError:
        return None
    finally:
        if parent_fd >= 0:
            _close(parent_fd)


def _read_member_at(parent_fd: int, leaf: str, component: str, limit: int) -> bytes:
    leaf_fd = _call(os.open, leaf, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        member_fd = _call(os.open, component, _leaf_flags(), dir_fd=leaf_fd)
        try:
            if not stat.S_ISREG(os.fstat(member_fd).st_mode):
                raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
            data = _read_upto(member_fd, limit + 1)
        finally:
            _close(member_fd)
    finally:
        _close(leaf_fd)
    if len(data) > limit:
        raise LeafTooLarge(errno.EFBIG)
    return data


def _read_upto(member_fd: int, size: int) -> bytes:
    parts: list[bytes] = []
    got = 0
    while got < size:
        chunk = _call(os.read, member_fd, min(_CHUNK, size - got))
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)

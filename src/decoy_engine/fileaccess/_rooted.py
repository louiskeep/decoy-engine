"""`RootedFileAccess`: a generic deny-by-default policy over pre-opened root directories.

Pattern: Capsicum and WASI preopens (a process reaches files only through directory
descriptors it was granted), with `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS)` as the confinement, as in `cap-std` and Go 1.24 `os.Root`.
There is no symlink opt-in. Off Linux it refuses to construct unless
`allow_portable_fallback=True`, which is racy and documented as such.

The handles a strict policy issues live here too: a handle over an authorized descriptor has
`library_path()` of `/proc/self/fd/<n>`, a reopen of the same inode with no further name
resolution, valid only while the descriptor stays open.
"""

from __future__ import annotations

import io
import os
import stat as stat_mod
import sys
import weakref
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, TextIO

from decoy_engine.fileaccess import _openat2
from decoy_engine.fileaccess._errors import deny
from decoy_engine.fileaccess._types import (
    Access,
    FileAccessUnavailable,
    FileHandle,
    FileRequest,
    FileRole,
    OutputDir,
)

_MAX_LOCATOR = 4096
_FD_DIR = "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"

_READ_FLAGS = os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class _PreadRaw(io.RawIOBase):
    """A seekable read-only view of a descriptor with its own offset (via `pread`), so two
    readers of one handle never share a file position."""

    def __init__(self, fd: int, name: str) -> None:
        self._fd = fd
        self._pos = 0
        self.name = name

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        data = os.pread(self._fd, len(buffer), self._pos)
        buffer[: len(data)] = data
        self._pos += len(data)
        return len(data)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = os.fstat(self._fd).st_size + offset
        return self._pos

    def tell(self) -> int:
        return self._pos


class FdHandle:
    """An open, authorized regular file. Closed on `close()` or when collected."""

    strict = True

    def __init__(self, fd: int, display: str) -> None:
        self._fd = fd
        self.display = display
        self._closer = weakref.finalize(self, _close_quietly, fd)

    def fileno(self) -> int:
        return self._fd

    def library_path(self) -> str:
        return f"{_FD_DIR}/{self._fd}"

    def stat(self) -> os.stat_result:
        return os.fstat(self._fd)

    def identity(self) -> str:
        """A key for "the same file" across calls: device and inode of the opened object."""
        st = os.fstat(self._fd)
        return f"{st.st_dev}:{st.st_ino}"

    def read_all(self, limit: int | None = None) -> bytes:
        chunks: list[bytes] = []
        offset = 0
        while limit is None or offset < limit:
            want = 1 << 20 if limit is None else min(1 << 20, limit - offset)
            data = os.pread(self._fd, want, offset)
            if not data:
                break
            chunks.append(data)
            offset += len(data)
        return b"".join(chunks)

    def open_binary(self, opener: object = None) -> BinaryIO:
        return io.BufferedReader(_PreadRaw(self._fd, self.display))

    def open_text(self, encoding: str = "utf-8") -> TextIO:
        return io.TextIOWrapper(self.open_binary(), encoding=encoding)

    def close(self) -> None:
        self._closer()

    def __enter__(self) -> FdHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_checked(
    dir_fd: int,
    rel: str,
    *,
    field_key: str,
    want: str,
    deny_inodes: frozenset[tuple[int, int]],
) -> int:
    """`openat2` confined beneath `dir_fd`, then `fstat`: a regular file (or a directory
    when `want` is `dir`), not a denylisted inode. Every failure is the one denial."""
    flags = _READ_FLAGS | (os.O_DIRECTORY if want == "dir" else 0)
    fd = -1
    try:
        fd = _openat2._confined_open(dir_fd, rel, flags)
    except OSError:
        pass
    ok = False
    if fd >= 0:
        try:
            st = os.fstat(fd)
            kind_ok = (
                stat_mod.S_ISDIR(st.st_mode)
                if want == "dir"
                else stat_mod.S_ISREG(st.st_mode)
                or (want == "any" and stat_mod.S_ISDIR(st.st_mode))
            )
            ok = kind_ok and (st.st_dev, st.st_ino) not in deny_inodes
        except OSError:
            ok = False
        if ok:
            _clear_nonblock(fd)
        else:
            _close_quietly(fd)
    if not ok:
        raise deny(field_key) from None
    return fd


def _clear_nonblock(fd: int) -> None:
    os.set_blocking(fd, True)


class FdDirHandle:
    """An open, authorized directory. Children are opened only beneath its own descriptor,
    so renaming the directory's path after open cannot redirect a later child open."""

    def __init__(
        self, fd: int, display: str, deny_inodes: frozenset[tuple[int, int]] = frozenset()
    ) -> None:
        self._fd = fd
        self.display = display
        self._deny = deny_inodes
        self._closer = weakref.finalize(self, _close_quietly, fd)

    def entries(self) -> Iterator[os.DirEntry[str]]:
        yield from os.scandir(self._fd)

    def open_file(self, rel: str) -> FdHandle:
        fd = open_checked(
            self._fd, rel, field_key=self.display, want="file", deny_inodes=self._deny
        )
        return FdHandle(fd, self.display)

    def open_dir(self, rel: str) -> FdDirHandle:
        fd = open_checked(self._fd, rel, field_key=self.display, want="dir", deny_inodes=self._deny)
        return FdDirHandle(fd, self.display, self._deny)

    def close(self) -> None:
        self._closer()

    def __enter__(self) -> FdDirHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class Root:
    """One pre-opened root directory and what it may serve.

    `prefix` maps a locator form to this root: a request whose locator begins with
    `prefix + "/"` is served from here with the prefix stripped. An empty prefix matches
    every locator for the roles and accesses listed."""

    name: str
    dir_fd: int
    roles: frozenset[FileRole]
    access: frozenset[str] = frozenset({"read", "stat", "read_dir"})
    prefix: str = ""
    path: str | None = field(default=None, compare=False)


def lexical_ok(locator: object) -> bool:
    """The pre-filter, never the gate: a non-empty `str` under 4096 chars with no NUL."""
    return isinstance(locator, str) and 0 < len(locator) <= _MAX_LOCATOR and "\0" not in locator


class RootedFileAccess:
    """Serves a request only from a root whose roles and access match, confined beneath it."""

    def __init__(
        self,
        roots: Iterable[Root],
        *,
        deny_inodes: Iterable[tuple[int, int]] = (),
        allow_portable_fallback: bool = False,
    ) -> None:
        if not _openat2.usable() and not allow_portable_fallback:
            raise FileAccessUnavailable(
                "RootedFileAccess needs openat2 (Linux 5.6+); pass allow_portable_fallback=True "
                "to confine with a descriptor walk instead"
            )
        self.roots: tuple[Root, ...] = tuple(roots)
        self.deny_inodes: frozenset[tuple[int, int]] = frozenset(deny_inodes)

    def allows_subprocess(self) -> bool:
        return False

    def select(self, req: FileRequest) -> tuple[Root, str]:
        """The root and root-relative path for `req`, decided without touching the
        filesystem. Anything unmatched is the one denial."""
        if not lexical_ok(req.locator):
            raise deny(req.field_key)
        for root in self.roots:
            if req.role not in root.roles or req.access not in root.access:
                continue
            if not root.prefix:
                return root, req.locator
            lead = root.prefix.rstrip("/") + "/"
            if req.locator.startswith(lead) and len(req.locator) > len(lead):
                return root, req.locator[len(lead) :]
        raise deny(req.field_key)

    def open_beneath(self, root: Root, rel: str, *, field_key: str, want: str = "file") -> int:
        """An authorized descriptor for `rel` beneath `root` (platform policies call this
        once they have mapped a locator to a root themselves)."""
        if not lexical_ok(rel):
            raise deny(field_key)
        return open_checked(
            root.dir_fd, rel, field_key=field_key, want=want, deny_inodes=self.deny_inodes
        )

    def open_read(self, req: FileRequest) -> FileHandle:
        if req.access not in ("read", "stat"):
            raise deny(req.field_key)
        root, rel = self.select(req)
        fd = self.open_beneath(root, rel, field_key=req.field_key)
        return FdHandle(fd, req.field_key)

    def open_dir(self, req: FileRequest) -> FdDirHandle:
        if req.access not in ("read_dir", "read"):
            raise deny(req.field_key)
        root, rel = self.select(req)
        fd = self.open_beneath(root, rel, field_key=req.field_key, want="dir")
        return FdDirHandle(fd, req.field_key, self.deny_inodes)

    def stat(self, req: FileRequest) -> os.stat_result:
        if req.access not in ("stat", "read_dir", "read"):
            raise deny(req.field_key)
        root, rel = self.select(req)
        fd = self.open_beneath(root, rel, field_key=req.field_key, want="any")
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)

    def output_dir(self, req: FileRequest) -> OutputDir:
        from decoy_engine.fileaccess._output import RootedOutputDir

        if req.access != "write":
            raise deny(req.field_key)
        root, rel = self.select(req)
        return RootedOutputDir(root.dir_fd, rel, req)


__all__ = ["Access", "Root", "RootedFileAccess", "lexical_ok"]

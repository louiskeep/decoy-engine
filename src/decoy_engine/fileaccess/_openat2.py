"""`openat2(2)` through `ctypes` (Linux 5.6+, syscall 437 on every architecture).

Pattern: `cap-std` and Go 1.24 `os.Root`. Resolution and the containment decision happen in
one kernel call (`RESOLVE_BENEATH`), which removes the check-then-open window (CWE-367).
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys

from decoy_engine.fileaccess._component import is_valid_component

RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
CONFINED = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS

_SYS_OPENAT2 = 437
_AT_FDCWD = -100


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


_syscall = None


def _bind() -> object:
    global _syscall
    if _syscall is None:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.syscall
        fn.restype = ctypes.c_long
        fn.argtypes = [
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        _syscall = fn
    return _syscall


def openat2(dir_fd: int, rel: str, flags: int, mode: int = 0, resolve: int = CONFINED) -> int:
    """Open `rel` beneath `dir_fd`. Raises `OSError` with the kernel errno on any refusal."""
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOSYS, "openat2 is Linux only")
    if "\0" in rel:
        raise OSError(errno.EINVAL, "embedded NUL")
    how = _OpenHow(flags | os.O_CLOEXEC, mode, resolve)
    fn = _bind()
    while True:
        fd = fn(  # type: ignore[operator]
            _SYS_OPENAT2, dir_fd, os.fsencode(rel), ctypes.byref(how), ctypes.sizeof(how)
        )
        if fd >= 0:
            return int(fd)
        err = ctypes.get_errno()
        if err != errno.EINTR:
            raise OSError(err, os.strerror(err))


# Set by a test to exercise the descriptor-walk fallback on a kernel that has `openat2`.
FORCE_PORTABLE = False
_usable: bool | None = None


def _confined_open(dir_fd: int, rel: str, flags: int) -> int:
    """Open `rel` beneath `dir_fd` without leaving it and without following a link.

    One `openat2` call where the kernel has it. Elsewhere, a walk that opens each component with
    `O_NOFOLLOW` relative to the previous descriptor, which refuses a symlink at every step and
    cannot be redirected by a rename of an ancestor; `..`, `.`, empty and absolute forms are
    refused outright. Raises `OSError` on any refusal."""
    if not FORCE_PORTABLE and usable():
        return openat2(dir_fd, rel, flags)
    *directories, leaf = rel.split("/")
    if not is_valid_component(leaf) or not all(is_valid_component(part) for part in directories):
        raise OSError(errno.EXDEV, "path leaves the root")
    fd = os.dup(dir_fd)
    try:
        for part in directories:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = child
        return os.open(leaf, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    finally:
        os.close(fd)


def usable() -> bool:
    global _usable
    if _usable is None:
        _usable = openat2_usable()
    return _usable


def openat2_usable() -> bool:
    """True when this process can call `openat2` (kernel support and no seccomp refusal).

    The probe opens the filesystem root through the syscall itself, so it needs no
    descriptor and no path-resolving call of its own."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        fd = openat2(_AT_FDCWD, "/", os.O_RDONLY | os.O_DIRECTORY, resolve=0)
    except OSError:
        return False
    os.close(fd)
    return True

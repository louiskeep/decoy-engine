"""The one allocator for directories that hold Decoy's own rows (spill, staging, quarantine).

A directory made here is owner-only from its first instant: POSIX ``mkdir(0o700)`` then an
explicit ``chmod`` (the umask only narrows a mode, so the explicit call restores 0700); Windows
``CreateDirectoryW`` with a protected DACL for the current user and SYSTEM, so the directory
never exists without it. An existing directory is reused only after ``lstat`` shows a real
directory (no symlink or reparse point) owned by the current user with no group or other
access. An empty one that is too open is tightened; one that already holds files is refused,
because files written while it was open may have been read. Every failure is closed: no
fallback, no warning.

Policy stance: a caller-named path is followed only under the ambient file-access policy
(``require_ambient``). A directory ``protected_scratch_dir`` issued and has not removed, and
anything beneath it after normalization, is the engine's own and works under every policy; a
``..`` escape from it is a caller path. The set of live issued roots lives here and nowhere else.

Errors carry fixed text with no path and no OS message, and are raised outside any ``except``
block so ``__cause__`` and ``__context__`` are both ``None``. Named limits: the check and the
chmod are separate calls (a hostile same-user swap in between is out of V1 scope), and the
Windows branch is verified only by the Windows lane (L0.1w).
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from decoy_engine import reasons
from decoy_engine.errors import DecoyError
from decoy_engine.fileaccess._entry import require_ambient

_MODE = 0o700
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junction, mount point, placeholder
_ISSUED: set[str] = set()
_LOCK = threading.Lock()
_WHAT = "a protected scratch directory"


class ProtectedDirUnsafe(DecoyError, ValueError):  # noqa: N818 - the plan names
    """An existing directory cannot be trusted, or the request itself is unusable."""

    code = reasons.runtime.SCRATCH_UNSAFE

    def __init__(self) -> None:
        super().__init__("the protected directory is not safe to use.")


class ProtectedDirAclFailed(DecoyError, OSError):  # noqa: N818 - the plan names
    """The owner-only protection could not be applied; nothing was left behind."""

    code = reasons.runtime.SCRATCH_ACL_FAILED

    def __init__(self) -> None:
        super().__init__("the protected directory could not be made owner-only.")


# -- the issued-root registry ------------------------------------------------------------------


def _key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _live(key: str) -> bool:
    """True for a root this module issued and has not removed, or a path beneath one."""
    with _LOCK:
        return any(key == root or key.startswith(root + os.sep) for root in _ISSUED)


# -- filesystem calls, each with OS text and context stripped ----------------------------------


def _raise_errno(code: int) -> None:
    if code:
        raise OSError(code, os.strerror(code))


def _lstat(path: str) -> os.stat_result | None:
    code, found = 0, None
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        code = exc.errno or errno.EIO
    _raise_errno(code)
    return found


def _mkdir(path: str) -> None:
    code = 0
    try:
        os.mkdir(path, _MODE)
    except OSError as exc:
        code = exc.errno or errno.EIO
    _raise_errno(code)


def _chmod_private(path: str) -> None:
    failed = False
    try:
        os.chmod(path, _MODE)
    except OSError:
        failed = True
    if failed:
        raise ProtectedDirAclFailed


def _is_empty(path: str) -> bool:
    code, names = 0, ["?"]
    try:
        names = os.listdir(path)
    except OSError as exc:
        code = exc.errno or errno.EIO
    _raise_errno(code)
    return not names


def _remove(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


# -- checking and creating ---------------------------------------------------------------------


def _check(path: str, found: os.stat_result, *, tighten: bool) -> None:
    """Refuse anything but a real, owned, owner-only directory; tighten an empty open one."""
    reparse = getattr(found, "st_file_attributes", 0) & _REPARSE_POINT
    if stat.S_ISLNK(found.st_mode) or not stat.S_ISDIR(found.st_mode) or reparse:
        raise ProtectedDirUnsafe
    if sys.platform == "win32":
        owned, closed = _win_state(path)
    else:
        owned = found.st_uid == os.getuid()
        closed = not stat.S_IMODE(found.st_mode) & 0o077
    if not owned:
        raise ProtectedDirUnsafe
    if closed:
        return
    if not tighten or not _is_empty(path):
        raise ProtectedDirUnsafe
    if sys.platform == "win32":
        _win_tighten(path)
    else:
        _chmod_private(path)
    again = _lstat(path)
    if again is None:
        raise ProtectedDirUnsafe
    _check(path, again, tighten=False)


def _create_level(path: str) -> None:
    """Create one missing directory, protected at creation, then check what is there."""
    if _lstat(path) is not None:
        raise OSError(errno.EEXIST, os.strerror(errno.EEXIST))
    if sys.platform == "win32":
        _win_create(path)
    else:
        _mkdir(path)
        try:
            _chmod_private(path)
        except ProtectedDirAclFailed:
            _remove(path)  # no half-protected directory is left behind
            raise
    made = _lstat(path)
    if made is None:
        raise ProtectedDirUnsafe
    try:
        _check(path, made, tighten=False)
    except ProtectedDirUnsafe:
        _remove(path)
        raise


def _make(path: str, *, parents: bool) -> None:
    levels = [path]
    if parents:
        parent = os.path.dirname(path)
        while parent != levels[-1] and _lstat(parent) is None:
            levels.append(parent)
            parent = os.path.dirname(parent)
    for level in reversed(levels):
        _create_level(level)


def _open(path: str, *, create: bool, parents: bool) -> None:
    found = _lstat(path)
    if found is None:
        if not create:
            raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))
        raced = False
        try:
            _make(path, parents=parents)
        except FileExistsError:
            raced = True  # another opener won the creation; judge what it left
        if not raced:
            return
        found = _lstat(path)
        if found is None:
            raise ProtectedDirUnsafe
    _check(path, found, tighten=True)


# -- public API --------------------------------------------------------------------------------


def open_protected_dir(
    path: str | os.PathLike[str], *, create: bool, parents: bool = False
) -> Path:
    """A long-lived protected directory at ``path``: created, or checked and reused.

    Idempotent: a directory that passes is returned unchanged, so repeated opens do nothing. A
    caller path needs the ambient policy; a live issued root and its children do not."""
    if not _live(_key(path)):
        require_ambient(_WHAT)
    _open(os.fspath(path), create=create, parents=parents)
    return Path(path)


@contextmanager
def protected_scratch_dir(
    parent: str | os.PathLike[str] | None = None, prefix: str = "decoy-"
) -> Iterator[Path]:
    """A fresh protected directory under ``parent`` (default: the system temp directory),
    removed on every exit. While it is open, it and its children are the engine's own paths."""
    if os.sep in prefix or (os.altsep is not None and os.altsep in prefix):
        raise ProtectedDirUnsafe
    base = tempfile.gettempdir()
    if parent is not None:
        if not _live(_key(parent)):
            require_ambient(_WHAT)
        base = os.fspath(parent)
        if _lstat(base) is None:
            _make(base, parents=True)
    root = ""
    for _ in range(8):
        candidate = os.path.join(base, prefix + secrets.token_hex(8))
        clash = False
        try:
            _create_level(candidate)
        except FileExistsError:
            clash = True
        if not clash:
            root = candidate
            break
    if not root:
        raise OSError(errno.EEXIST, os.strerror(errno.EEXIST))
    key = _key(root)
    with _LOCK:
        _ISSUED.add(key)
    try:
        yield Path(root)
    finally:
        with _LOCK:
            _ISSUED.discard(key)
        _remove(root)


# -- Windows: owner-only DACL at creation, read back for reuse ---------------------------------

if sys.platform == "win32":  # pragma: no cover - exercised only on the Windows lane (L0.1w)
    import ctypes
    from ctypes import wintypes

    _SDDL_REVISION, _SE_FILE_OBJECT, _DACL, _OWNER = 1, 1, 0x4, 0x1
    _PROTECTED_DACL = 0x80000000
    _ACE = re.compile(r"\(([^)]*)\)")
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    _adv.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    def _token_sid(kind: int) -> str:
        """The string SID of the process token's user (kind 1) or default owner (kind 4)."""
        token, size = wintypes.HANDLE(), wintypes.DWORD()
        if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise ProtectedDirAclFailed
        try:
            _adv.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not _adv.GetTokenInformation(token, kind, buf, size, ctypes.byref(size)):
                raise ProtectedDirAclFailed
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            text = wintypes.LPWSTR()
            if not _adv.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
                raise ProtectedDirAclFailed
            out = text.value or ""
            _k32.LocalFree(text)
            return out
        finally:
            _k32.CloseHandle(token)

    def _descriptor() -> ctypes.c_void_p:
        sddl = f"D:P(A;OICI;FA;;;{_token_sid(1)})(A;OICI;FA;;;SY)"
        sd = ctypes.c_void_p()
        if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION, ctypes.byref(sd), None
        ):
            raise ProtectedDirAclFailed
        return sd

    def _win_create(path: str) -> None:
        sd = _descriptor()
        try:
            attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), sd, False)
            if not _k32.CreateDirectoryW(path, ctypes.byref(attrs)):
                exists = ctypes.get_last_error() in (80, 183)  # file / already exists
                _raise_errno(errno.EEXIST if exists else errno.EACCES)
        finally:
            _k32.LocalFree(sd)

    def _win_tighten(path: str) -> None:
        sd = _descriptor()
        try:
            present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
            found = _adv.GetSecurityDescriptorDacl(
                sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            )
            flags = _DACL | _PROTECTED_DACL
            if not found or _adv.SetNamedSecurityInfoW(
                path, _SE_FILE_OBJECT, flags, None, None, dacl, None
            ):
                raise ProtectedDirAclFailed
        finally:
            _k32.LocalFree(sd)

    def _win_state(path: str) -> tuple[bool, bool]:
        """(owned by this user, DACL protected and exactly this user plus SYSTEM)."""
        sd, text = ctypes.c_void_p(), wintypes.LPWSTR()
        if _adv.GetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, _OWNER | _DACL, None, None, None, None, ctypes.byref(sd)
        ):
            raise ProtectedDirAclFailed
        try:
            if not _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                sd, _SDDL_REVISION, _OWNER | _DACL, ctypes.byref(text), None
            ):
                raise ProtectedDirAclFailed
            sddl = text.value or ""
            _k32.LocalFree(text)
        finally:
            _k32.LocalFree(sd)
        me = _token_sid(1)
        owner = re.match(r"O:([^GD:]+)", sddl)
        owned = owner is not None and owner.group(1) in {me, _token_sid(4)}
        dacl = sddl.partition("D:")[2]
        aces = {tuple(a.split(";")) for a in _ACE.findall(dacl)}
        want = {("A", "OICI", "FA", "", "", me), ("A", "OICI", "FA", "", "", "SY")}
        return owned, dacl.startswith("P") and aces == want

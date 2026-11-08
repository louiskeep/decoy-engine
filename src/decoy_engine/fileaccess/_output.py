"""Policy-issued output transactions over directory descriptors (strict policies).

Pattern: `cap-std`'s `Dir` (create, rename and remove only through a held directory handle),
Spark's `FileOutputCommitter` (stage privately, publish with one rename, delete the stage on
abort) and POSIX `renameat`/`linkat` between directory fds. The caller never supplies or
sees a stage pathname: the stage has a random name beside the final leaf, so publication is
a rename on one filesystem. Every child name passes `validate_component` inside the
transaction, and every operation is one `dir_fd`-relative call with `O_NOFOLLOW`, so a
symlink planted in the stage is never followed.

Publication keeps each sink's existing semantics: a directory stage publishes with plain
`rename(2)` (an absent or empty destination, `ENOTEMPTY` on a non-empty one, stage intact for
`abort()`); a file stage publishes with `link(2)` (`exclusive`, refuses any existing leaf) or
`rename(2)` (`replace`). No path uses `RENAME_EXCHANGE`.
"""

from __future__ import annotations

import errno
import os
import secrets
import weakref
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from decoy_engine.fileaccess import _openat2
from decoy_engine.fileaccess._component import validate_component
from decoy_engine.fileaccess._errors import deny
from decoy_engine.fileaccess._types import FileAccessUnavailable, FileRequest, StageLabel

# The only names a caller can influence: a closed label picks a prefix, never a path.
STAGE_PREFIX: dict[str, str] = {
    "table_sink": "_decoy_stage_",
    "quarantine": "_decoy_quarantine_stage_",
    "quarantine_direct": "_decoy_quarantine_stage_",
    "isolated": "_decoy_isolated_stage_",
    "hold": "_decoy_hold_",
    "subset": "_decoy_subset_stage_",
    "vault": "_decoy_vault_stage_",
    "pack": "_decoy_pack_stage_",
}
STAGE_SUFFIX: dict[str, str] = {"quarantine": ".jsonl", "quarantine_direct": ".jsonl"}
# Labels whose stage holds secrets or real data, so the stage file is 0600 at creation.
PRIVATE_LABELS: frozenset[str] = frozenset({"quarantine", "isolated"})
# Every output but a vault leaf may create missing parent directories; a vault leaf sits
# directly in a directory the caller already provided.
CREATES_PARENTS: frozenset[str] = frozenset(STAGE_PREFIX) - {"vault"}

T = TypeVar("T")

# Denial key for a bad name given straight to a creator; it names the capability, never the name.
_CREATOR_KEY = "fileaccess.stage"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run one syscall wrapper. A failure keeps only its errno: the stock `OSError` text
    names the stage and leaf, and a basename must not reach an error message."""
    code = 0
    try:
        return fn(*args, **kwargs)
    except OSError as exc:
        code = exc.errno or errno.EIO
    raise OSError(code, os.strerror(code)) from None


def make_stage_dir_at(parent_fd: int, name: str) -> int:
    """Create directory ``name`` under ``parent_fd``, owner-readable-writable whatever the umask,
    and return it opened. The umask only narrows ``mkdir``'s mode: 0777 leaves the directory
    0000 (the open fails ``EACCES``, one ``chmod`` repairs it) and 0200 leaves it 0500 (an
    ``fchmod`` restores the owner bits). A mode-ignoring filesystem already shows them, so
    nothing is changed there. Failures keep only their errno; the directory this call made is
    removed if a later step fails, and an existing entry is never touched."""
    validate_component(name, _CREATOR_KEY)
    _call(os.mkdir, name, 0o700, dir_fd=parent_fd)
    fd = -1
    try:
        try:
            fd = _call(os.open, name, _DIR_FLAGS, dir_fd=parent_fd)
        except PermissionError:
            # Named limit L6: this chmod goes by name, so a different user who can write in the
            # caller's output directory could swap a symlink in between mkdir and here. The effect
            # stays bounded: 0700 on a path the engine user owns, and no read. An fd-only repair
            # is impossible: a 0000 directory cannot be opened, and fchmod on an O_PATH
            # descriptor gives EBADF. Reached only for a directory this call just created (mkdir
            # raises, EEXIST included, before this point).
            _call(os.chmod, name, 0o700, dir_fd=parent_fd)
            fd = _call(os.open, name, _DIR_FLAGS, dir_fd=parent_fd)
        if (os.fstat(fd).st_mode & 0o700) != 0o700:
            _call(os.fchmod, fd, 0o700)
    except OSError:
        _close(fd)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    return fd


def _restore_always(fd: int) -> None:
    _call(os.fchmod, fd, 0o600)


def _restore_missing_owner_bits(fd: int) -> None:
    """Repair only what the umask stripped, so a filesystem that already reports owner read and
    write is not asked for a chmod it may refuse."""
    if (_call(os.fstat, fd).st_mode & 0o600) != 0o600:
        _call(os.fchmod, fd, 0o600)


def _create_private_core(dir_fd: int, name: str, restore: Callable[[int], None]) -> int:
    """The one private-file creation: ``O_EXCL`` so only this call's file is ever touched, then
    the caller's restorer. If the restorer fails, close and unlink the name this call created."""
    fd = _call(os.open, name, _FILE_CREATE, 0o600, dir_fd=dir_fd)
    try:
        restore(fd)
    except OSError:
        _close(fd)
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    return fd


def create_private_at(dir_fd: int, name: str) -> int:
    """A new 0600 file whose mode is set unconditionally (top-level private stages, arena files)."""
    validate_component(name, _CREATOR_KEY)
    return _create_private_core(dir_fd, name, _restore_always)


def create_private_child_at(dir_fd: int, name: str) -> int:
    """A new private file inside a stage directory: owner bits restored only when missing."""
    validate_component(name, _CREATOR_KEY)
    return _create_private_core(dir_fd, name, _restore_missing_owner_bits)


def create_output_file_at(dir_fd: int, name: str, mode: int) -> int:
    """A new output-mode file; the umask governs ``mode``."""
    validate_component(name, _CREATOR_KEY)
    return _call(os.open, name, _FILE_CREATE, mode, dir_fd=dir_fd)


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _sync(dir_fd: int) -> None:
    """Best-effort fsync of the parent directory so a file rename survives a crash; a
    failure here never fails an output that has already been published."""
    try:
        os.fsync(dir_fd)
    except OSError:
        pass


def _rmtree_fd(dir_fd: int) -> None:
    """Remove everything beneath `dir_fd` without following any link."""
    for entry in list(os.scandir(dir_fd)):
        if entry.is_dir(follow_symlinks=False):
            child = os.open(entry.name, _DIR_FLAGS, dir_fd=dir_fd)
            try:
                _rmtree_fd(child)
            finally:
                _close(child)
            os.rmdir(entry.name, dir_fd=dir_fd)
        else:
            os.unlink(entry.name, dir_fd=dir_fd)


class _State:
    """What a transaction owns, held apart from the object so collection can clean up."""

    def __init__(self, parent_fd: int, stage_name: str, kind: str) -> None:
        self.parent_fd = parent_fd
        self.stage_name = stage_name
        self.kind = kind
        self.stage_fd = -1
        self.finished = False
        self.file_created = False
        self.child_fds: list[int] = []


def _close_children(state: _State) -> None:
    while state.child_fds:
        _close(state.child_fds.pop())


def _discard(state: _State) -> None:
    if state.finished:
        return
    state.finished = True
    try:
        if state.kind == "dir":
            if state.stage_fd >= 0:
                try:
                    _rmtree_fd(state.stage_fd)
                except OSError:
                    pass
                _close(state.stage_fd)
            try:
                os.rmdir(state.stage_name, dir_fd=state.parent_fd)
            except OSError:
                pass
        else:
            try:
                os.unlink(state.stage_name, dir_fd=state.parent_fd)
            except OSError:
                pass
    finally:
        state.stage_fd = -1
        _close_children(state)
        _close(state.parent_fd)


class _StageDir:
    """Name-taking operations over one stage directory descriptor."""

    strict = True

    def __init__(self, dir_fd: int, display: str, live: _State, mode_bits: int) -> None:
        self._fd = dir_fd
        self.display = display
        self._display = display
        self._live = live
        self._mode = mode_bits

    def _check(self, *components: str) -> None:
        if self._live.finished:
            raise FileAccessUnavailable("output transaction is finished")
        for component in components:
            validate_component(component, self._display)

    def create_file(self, component: str) -> int:
        self._check(component)
        if self._mode == 0o600:
            return create_private_child_at(self._fd, component)
        return create_output_file_at(self._fd, component, self._mode)

    def open_file(self, component: str) -> int:
        self._check(component)
        return _call(
            os.open, component, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._fd
        )

    def create_dir(self, component: str) -> _StageDir:
        self._check(component)
        child = make_stage_dir_at(self._fd, component)
        self._live.child_fds.append(child)
        return _StageDir(child, self._display, self._live, self._mode)

    def remove(self, component: str) -> None:
        self._check(component)
        try:
            _call(os.unlink, component, dir_fd=self._fd)
        except IsADirectoryError:
            _call(os.rmdir, component, dir_fd=self._fd)

    def rename(self, src: str, dst: str) -> None:
        self._check(src, dst)
        _call(os.rename, src, dst, src_dir_fd=self._fd, dst_dir_fd=self._fd)

    def local_stage_path(self) -> str:
        raise FileAccessUnavailable("a strict policy has no stage path to hand to a subprocess")

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError(
            "an output transaction cannot be serialized; it crosses a process as its fd"
        )


_ISSUER = object()


class RootedOutputTxn(_StageDir):
    """The top-level stage: publishes once or aborts, never both. Only `RootedOutputDir.begin`
    can make one: the issuer token is private to this module, so a transaction built any other
    way is refused before it touches a descriptor."""

    def __init__(
        self,
        issuer: object,
        parent_fd: int,
        leaf: str,
        kind: Literal["file", "dir"],
        mode: Literal["exclusive", "replace"],
        label: StageLabel,
        display: str,
    ) -> None:
        if issuer is not _ISSUER:
            raise FileAccessUnavailable("an output transaction is issued by an OutputDir")
        validate_component(leaf, display)
        if label not in STAGE_PREFIX:
            raise deny(display)  # an in-place label exists only for the ambient policy
        self._leaf = leaf
        self._publish_mode = mode
        name = f"{STAGE_PREFIX[label]}{secrets.token_hex(6)}{STAGE_SUFFIX.get(label, '')}"
        state = _State(os.dup(parent_fd), name, kind)
        self._state = state
        self._private = label in PRIVATE_LABELS
        mode_bits = 0o600 if self._private else 0o666
        stage_fd = -1
        if kind == "dir":
            stage_fd = make_stage_dir_at(state.parent_fd, name)
            state.stage_fd = stage_fd
        super().__init__(stage_fd, display, state, mode_bits)
        self._finalizer = weakref.finalize(self, _discard, state)

    def create_file(self, component: str) -> int:
        if self._state.kind == "file":
            self._check(component)
            if self._state.file_created:
                raise FileAccessUnavailable("a file stage holds exactly one file")
            self._state.file_created = True
            if self._private:
                return create_private_at(self._state.parent_fd, self._state.stage_name)
            return create_output_file_at(self._state.parent_fd, self._state.stage_name, self._mode)
        return super().create_file(component)

    def publish(self) -> None:
        state = self._state
        if state.finished:
            raise FileAccessUnavailable("output transaction is finished")
        if state.kind == "file" and not state.file_created:
            raise FileAccessUnavailable("nothing was staged")
        parent = state.parent_fd
        if state.kind == "file" and self._publish_mode == "exclusive":
            try:
                _call(
                    os.link,
                    state.stage_name,
                    self._leaf,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                _discard(state)
                raise
            try:
                os.unlink(state.stage_name, dir_fd=parent)
            except OSError:
                pass
        else:
            _call(os.replace, state.stage_name, self._leaf, src_dir_fd=parent, dst_dir_fd=parent)
            if state.kind == "file":
                _sync(parent)
        state.finished = True
        if state.stage_fd >= 0:
            _close(state.stage_fd)
            state.stage_fd = -1
        _close_children(state)
        _close(parent)
        self._finalizer.detach()

    def abort(self) -> None:
        _discard(self._state)
        self._finalizer.detach()

    def __enter__(self) -> RootedOutputTxn:
        return self

    def __exit__(self, *exc: object) -> None:
        self.abort()


class RootedOutputDir:
    """An output root fd plus one allowed leaf. Parent directories are created one validated
    component at a time with `mkdirat` and `openat2`; no symlink is ever followed."""

    strict = True

    def __init__(self, root_fd: int, rel: str, req: FileRequest) -> None:
        parts = rel.split("/")
        if any(p in ("", ".") for p in parts[:-1]) or not parts:
            raise deny(req.field_key)
        for part in parts:
            validate_component(part, req.field_key)
        self._root_fd = root_fd
        self._parents = parts[:-1]
        self._leaf = parts[-1]
        self.display = req.field_key
        self.locator = req.locator
        self._field_key = req.field_key

    def _open_parent(self, *, create: bool) -> int:
        fd = os.dup(self._root_fd)
        try:
            for part in self._parents:
                if create:
                    try:
                        os.mkdir(part, 0o777, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = _openat2._confined_open(fd, part, os.O_RDONLY | os.O_DIRECTORY)
                _close(fd)
                fd = child
        except OSError:
            _close(fd)
            raise
        return fd

    def begin(
        self,
        kind: Literal["file", "dir"],
        mode: Literal["exclusive", "replace"],
        *,
        label: StageLabel,
    ) -> RootedOutputTxn:
        failed = False
        parent = -1
        try:
            parent = self._open_parent(create=label in CREATES_PARENTS)
        except OSError:
            failed = True
        if failed:
            raise deny(self._field_key) from None
        try:
            return RootedOutputTxn(_ISSUER, parent, self._leaf, kind, mode, label, self.display)
        finally:
            _close(parent)

    def ensure_dir(self) -> None:
        failed = False
        parent = -1
        try:
            parent = self._open_parent(create=True)
            try:
                os.mkdir(self._leaf, 0o777, dir_fd=parent)
            except FileExistsError:
                child = _openat2._confined_open(parent, self._leaf, os.O_RDONLY | os.O_DIRECTORY)
                _close(child)
        except OSError:
            failed = True
        finally:
            if parent >= 0:
                _close(parent)
        if failed:
            raise deny(self._field_key) from None

    def is_empty_or_absent(self) -> bool:
        failed = False
        parent = child = -1
        try:
            parent = self._open_parent(create=False)
            child = _openat2._confined_open(parent, self._leaf, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            return True
        except OSError:
            failed = True
        finally:
            if parent >= 0:
                _close(parent)
        if failed:  # raised outside the handler: the OSError (which names the leaf) never rides
            raise deny(self._field_key)
        try:
            with os.scandir(child) as it:
                return next(it, None) is None
        finally:
            _close(child)


__all__ = [
    "PRIVATE_LABELS",
    "STAGE_PREFIX",
    "STAGE_SUFFIX",
    "RootedOutputDir",
    "RootedOutputTxn",
]

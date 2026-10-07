"""`LocalFileAccess`: today's ambient semantics, the no-policy CLI default.

Paths are plain paths relative to the CWD, symlinks are followed, `display` is the path, and
every open is lazy so a missing file fails inside the library call with the same text it
always had. Output transactions use ordinary path calls (`tempfile.mkdtemp` in the target's
parent, one `os.replace`), which is exactly what the sinks did before the policy existed.
This module is the one place ambient opens of caller or config paths are allowed.
"""

from __future__ import annotations

import errno
import logging
import logging.handlers
import os
import secrets
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO, Literal, TextIO

from decoy_engine.fileaccess._errors import deny
from decoy_engine.fileaccess._output import CREATES_PARENTS, STAGE_PREFIX, STAGE_SUFFIX
from decoy_engine.fileaccess._types import (
    FileAccessUnavailable,
    FileRequest,
    StageLabel,
)

_logger = logging.getLogger("decoy_engine.quarantine")

_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
_CREATE_IN_PLACE = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC


class LocalHandle:
    """A path standing in for an open file; nothing is opened until a library asks."""

    strict = False

    def __init__(self, path: str) -> None:
        self._path = path
        self.display = path
        self._fd: int | None = None

    def fileno(self) -> int:
        if self._fd is None:
            self._fd = os.open(self._path, os.O_RDONLY | os.O_CLOEXEC)
        return self._fd

    def library_path(self) -> str:
        return self._path

    def stat(self) -> os.stat_result:
        return Path(self._path).stat()

    def identity(self) -> str:
        """A key for "the same file" across calls: the resolved path, as the CLI always keyed."""
        return str(Path(self._path).resolve())

    def read_all(self, limit: int | None = None) -> bytes:
        with open(self._path, "rb") as fh:
            return fh.read() if limit is None else fh.read(limit)

    def open_binary(self, opener: Callable[..., BinaryIO] | None = None) -> BinaryIO:
        """`opener` lets a caller that already routes `open` through a seam keep doing so."""
        return (opener or open)(self._path, "rb")

    def open_text(self, encoding: str = "utf-8") -> TextIO:
        return open(self._path, encoding=encoding)

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> LocalHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def engine_handle(path: str | os.PathLike[str]) -> LocalHandle:
    """A handle over a file the engine itself owns: shipped package data, or a scratch file
    it created in its own temp space.

    Never for a path that came from a config or a caller. Every call site is listed in the
    sentry registry with the rule that built its path."""
    return LocalHandle(os.fspath(path))


def engine_dir_handle(path: str | os.PathLike[str]) -> LocalDirHandle:
    """A directory handle over package data the engine ships (a model pack, a hint library).

    Never for a path that came from a config or a caller; every call site is listed in the
    sentry registry with the rule that built its path."""
    return LocalDirHandle(os.fspath(path))


class LocalDirHandle:
    def __init__(self, path: str) -> None:
        self._path = path
        self.display = path

    def entries(self) -> Iterator[os.DirEntry[str]]:
        yield from os.scandir(self._path)

    def open_file(self, rel: str) -> LocalHandle:
        return LocalHandle(os.path.join(self._path, rel))

    def open_dir(self, rel: str) -> LocalDirHandle:
        return LocalDirHandle(os.path.join(self._path, rel))

    def close(self) -> None:
        return None

    def __enter__(self) -> LocalDirHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _LocalStageDir:
    """Name-taking operations over one stage directory, by path."""

    strict = False

    def __init__(self, path: Path, display: str) -> None:
        self._dir = path
        self.display = display
        self._display = display
        self._finished = False

    def _check(self, *components: str) -> None:
        """Refuse a name that would leave the stage. Length and NUL are left to the operating
        system so the ambient policy raises the same errors a plain path write always did."""
        if self._finished:
            raise FileAccessUnavailable("output transaction is finished")
        for component in components:
            if not component or component in (".", "..") or "/" in component:
                raise deny(self._display)

    def create_file(self, component: str) -> int:
        self._check(component)
        return os.open(self._dir / component, _CREATE, 0o666)

    def open_file(self, component: str) -> int:
        self._check(component)
        return os.open(self._dir / component, os.O_RDONLY | os.O_CLOEXEC)

    def create_dir(self, component: str) -> _LocalStageDir:
        self._check(component)
        (self._dir / component).mkdir()
        return _LocalStageDir(self._dir / component, self._display)

    def remove(self, component: str) -> None:
        self._check(component)
        target = self._dir / component
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            os.unlink(target)

    def rename(self, src: str, dst: str) -> None:
        self._check(src, dst)
        os.rename(self._dir / src, self._dir / dst)

    def local_stage_path(self) -> str:
        return str(self._dir)


class LocalOutputTxn(_LocalStageDir):
    """The legacy stage: a `mkdtemp` directory (or one `mkstemp` file) in the target's parent."""

    def __init__(
        self,
        target: Path,
        kind: Literal["file", "dir"],
        mode: Literal["exclusive", "replace"],
        label: StageLabel,
    ) -> None:
        self._target = target
        self._kind = kind
        self._mode = mode
        self._label = label
        self._staged_file: Path | None = None
        self._direct = False
        stage: Path | None = None
        if kind == "dir":
            if label in CREATES_PARENTS:
                target.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX[label], dir=target.parent))
        super().__init__(stage if stage is not None else target.parent, str(target))
        self._stage = stage

    def create_file(self, component: str) -> int:
        if self._kind == "file":
            self._check(component)
            if self._staged_file is not None:
                raise FileAccessUnavailable("a file stage holds exactly one file")
            if self._label == "vault_direct":
                # The ambient vault write truncates and rewrites the named file in place,
                # as `Path.write_bytes` always did (hardlinks, a read-only directory and a
                # missing parent behave as before). Only this policy issues the label.
                self._direct = True
                self._staged_file = self._target
                return os.open(self._target, _CREATE_IN_PLACE, 0o666)
            if self._label in CREATES_PARENTS:
                self._target.parent.mkdir(parents=True, exist_ok=True)
            if self._mode == "replace" and self._writes_through():
                self._direct = True
                self._staged_file = self._target
                return os.open(self._target, os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC)
            prefix = STAGE_PREFIX[self._label]
            suffix = STAGE_SUFFIX.get(self._label, "")
            if self._label == "quarantine":
                fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=self._target.parent)
                self._staged_file = Path(name)
                return fd
            stage = self._target.parent / f"{prefix}{secrets.token_hex(6)}{suffix}"
            self._staged_file = stage
            return os.open(stage, _CREATE, 0o666)
        return super().create_file(component)

    def _writes_through(self) -> bool:
        """An existing special file (`/dev/null`) or symlink is written in place, as the
        un-staged writer always did, because a stage beside it cannot be created or renamed
        over it."""
        return self._target.is_symlink() or (self._target.exists() and not self._target.is_file())

    def local_stage_path(self) -> str:
        if self._stage is None:
            raise FileAccessUnavailable("a file stage has no directory path")
        return str(self._stage)

    def publish(self) -> None:
        if self._finished:
            raise FileAccessUnavailable("output transaction is finished")
        if self._kind == "dir":
            if self._stage is None:  # pragma: no cover - a dir stage always has one
                raise FileAccessUnavailable("output transaction is finished")
            self._target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self._stage, self._target)
            self._stage = None
            self._finished = True
            return
        staged = self._staged_file
        if staged is None:
            raise FileAccessUnavailable("nothing was staged")
        if self._direct:
            self._finished = True
            return
        if self._mode == "replace":
            os.replace(staged, self._target)
            self._finished = True
            return
        try:
            os.link(staged, self._target)
        except FileExistsError:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
            self._finished = True
            raise FileExistsError(errno.EEXIST, "output exists") from None
        self._finished = True
        try:
            os.unlink(staged)
        except OSError as exc:
            _logger.warning(
                "quarantine sidecar published but failed to remove the staging link (%s); "
                "the sidecar is intact and the leftover staging file is harmless",
                type(exc).__name__,
            )

    def abort(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._stage is not None:
            shutil.rmtree(self._stage, ignore_errors=True)
            self._stage = None
        if self._staged_file is not None and not self._direct:
            try:
                self._staged_file.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> LocalOutputTxn:
        return self

    def __exit__(self, *exc: object) -> None:
        self.abort()


class LocalOutputDir:
    strict = False

    def __init__(self, req: FileRequest) -> None:
        self._target = Path(req.locator)
        self.display = req.locator
        self.locator = req.locator

    def begin(
        self,
        kind: Literal["file", "dir"],
        mode: Literal["exclusive", "replace"],
        *,
        label: StageLabel,
    ) -> LocalOutputTxn:
        return LocalOutputTxn(self._target, kind, mode, label)

    def ensure_dir(self) -> None:
        self._target.parent.mkdir(parents=True, exist_ok=True)
        self._target.mkdir(exist_ok=True)

    def is_empty_or_absent(self) -> bool:
        return not self._target.exists() or not any(self._target.iterdir())


class LocalFileAccess:
    """Constructed only inside `fileaccess/_entry.py`; never installed after `forbid_ambient()`."""

    def open_read(self, req: FileRequest) -> LocalHandle:
        return LocalHandle(req.locator)

    def open_dir(self, req: FileRequest) -> LocalDirHandle:
        return LocalDirHandle(req.locator)

    def stat(self, req: FileRequest) -> os.stat_result:
        return os.stat(req.locator)

    def output_dir(self, req: FileRequest) -> LocalOutputDir:
        return LocalOutputDir(req)

    def allows_subprocess(self) -> bool:
        return True

    def log_handler(self, locator: str, max_bytes: int, backup_count: int) -> logging.Handler:
        """The rotating file handler for the CLI's log file; ambient policy only, because a
        log is appended to for the life of the process and is not an output transaction."""
        parent = os.path.dirname(locator)
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        return logging.handlers.RotatingFileHandler(
            locator, maxBytes=max_bytes, backupCount=backup_count
        )


__all__ = ["LocalFileAccess", "engine_dir_handle", "engine_handle"]

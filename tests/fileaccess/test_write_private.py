"""`fileaccess.write_private`: 0600 from creation, atomic replace, value-free errors (X7b AT-P1).

Umask tests restore the process umask in `finally`; they change process state and must not
share a process with tests running in parallel threads.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccess, FileRequest, FileRole, LocalFileAccess
from decoy_engine.fileaccess._local import LocalOutputTxn
from tests.fileaccess.support import RootRelative

MARKER = "zz_marker"
STAGE = "_decoy_isolated_stage_"
OLD = b"old bytes"


@contextmanager
def _umask(mask: int) -> Iterator[None]:
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture(params=["ambient", "strict"])
def policy(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[FileAccess]:
    if request.param == "ambient":
        yield LocalFileAccess()
        return
    strict = RootRelative(tmp_path)
    try:
        yield strict
    finally:
        strict.close()


def _req(target: Path, key: str = "isolated.payload") -> FileRequest:
    return FileRequest(FileRole.CONFIG, "write", str(target), key)


def _write(policy: FileAccess, target: Path, data: bytes, key: str = "isolated.payload") -> None:
    with fileaccess.scope(policy):
        fileaccess.write_private(_req(target, key), data)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _stages(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(STAGE))


def _closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False


def test_mode_is_0600_before_any_fchmod_under_umask_000(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o777)
    seen: list[int] = []
    real_fchmod, real_write = os.fchmod, os.write

    def fchmod_spy(fd: int, mode: int) -> None:
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fchmod(fd, mode)

    def write_spy(fd: int, data: Any) -> int:
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_write(fd, data)

    monkeypatch.setattr(os, "fchmod", fchmod_spy)
    monkeypatch.setattr(os, "write", write_spy)
    with _umask(0):
        _write(LocalFileAccess(), tmp_path / "payload.json", b"secret")
    monkeypatch.undo()
    assert seen and seen[0] == 0o600, seen
    assert _mode(tmp_path / "payload.json") == 0o600


def test_usable_and_0600_under_umask_0777(policy: FileAccess, tmp_path: Path) -> None:
    target = tmp_path / "payload.json"
    with _umask(0o777):
        _write(policy, target, b"secret")
    assert _mode(target) == 0o600
    with open(target, "rb") as fh:
        assert fh.read() == b"secret"


def test_strict_fchmod_failure_fails_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "payload.json"
    target.write_bytes(OLD)
    strict = RootRelative(tmp_path)
    stage_fds: list[int] = []

    def fchmod_fails(fd: int, mode: int) -> None:
        stage_fds.append(fd)
        raise OSError(errno.EPERM, "Operation not permitted", MARKER)

    monkeypatch.setattr(os, "fchmod", fchmod_fails)
    try:
        with pytest.raises(OSError) as info:
            _write(strict, target, b"secret")
    finally:
        monkeypatch.undo()
        strict.close()
    assert info.value.errno == errno.EPERM
    assert MARKER not in str(info.value) and MARKER not in repr(info.value)
    assert target.read_bytes() == OLD
    assert _stages(tmp_path) == []
    assert stage_fds and all(_closed(fd) for fd in stage_fds)


class _SpinSentinelError(Exception):
    """Raised by the zero-progress fake once it has spun long enough to prove a loop."""


def _zero_progress() -> Callable[..., int]:
    calls = [0]

    def write(fd: int, data: Any) -> int:
        calls[0] += 1
        if calls[0] > 1000:
            raise _SpinSentinelError()
        return 0

    return write


def _partial(real: Callable[..., int]) -> Callable[..., int]:
    calls = [0]

    def write(fd: int, data: Any) -> int:
        calls[0] += 1
        if calls[0] == 1:
            return real(fd, bytes(data[:1]))
        raise OSError(errno.ENOSPC, "No space left on device")

    return write


def _fault(step: str, recorded: list[int], marker_path: str) -> tuple[str, Callable[..., Any], int]:
    """(os attribute, fake, expected errno) for one failing step."""
    real_write = os.write

    def fail_on_fd(code: int) -> Callable[..., Any]:
        def fake(fd: int, *_a: Any) -> Any:
            recorded.append(fd)
            raise OSError(code, os.strerror(code), marker_path)

        return fake

    if step == "fchmod":
        return "fchmod", fail_on_fd(errno.EPERM), errno.EPERM
    if step == "write":
        return "write", fail_on_fd(errno.ENOSPC), errno.ENOSPC
    if step == "partial":
        return "write", _recording(_partial(real_write), recorded), errno.ENOSPC
    if step == "zero":
        return "write", _recording(_zero_progress(), recorded), errno.EIO
    if step == "fsync":
        return "fsync", fail_on_fd(errno.EIO), errno.EIO
    if step == "close":
        real_close = os.close

        def close(fd: int) -> None:
            recorded.append(fd)
            real_close(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO), marker_path)

        return "close", close, errno.EIO

    def replace(src: Any, dst: Any, **_k: Any) -> None:
        raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(src), str(dst))

    return "replace", replace, errno.EACCES


def _recording(fake: Callable[..., int], recorded: list[int]) -> Callable[..., int]:
    def wrapper(fd: int, data: Any) -> int:
        recorded.append(fd)
        return fake(fd, data)

    return wrapper


STEPS = ["fchmod", "write", "partial", "zero", "fsync", "replace"]


@pytest.mark.parametrize("step", STEPS)
def test_failures_leave_the_existing_target_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    target = tmp_path / "payload.json"
    target.write_bytes(OLD)
    target.chmod(0o644)
    fds: list[int] = []
    real_create = LocalOutputTxn.create_file

    def create(self: LocalOutputTxn, component: str) -> int:
        fd = real_create(self, component)
        fds.append(fd)
        return fd

    monkeypatch.setattr(LocalOutputTxn, "create_file", create)
    name, fake, code = _fault(step, fds, str(target))
    monkeypatch.setattr(os, name, fake)
    try:
        with pytest.raises(BaseException) as info:
            _write(LocalFileAccess(), target, b"new secret bytes")
    finally:
        monkeypatch.undo()
    assert isinstance(info.value, OSError), type(info.value).__name__
    assert info.value.errno == code
    assert target.read_bytes() == OLD
    assert _mode(target) == 0o644
    assert _stages(tmp_path) == []
    assert all(_closed(fd) for fd in fds)


def test_success_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "payload.json"
    target.write_bytes(OLD)
    target.chmod(0o644)
    _write(LocalFileAccess(), target, b"new")
    assert target.read_bytes() == b"new"
    assert _mode(target) == 0o600
    empty = tmp_path / "empty.json"
    _write(LocalFileAccess(), empty, b"")
    assert empty.read_bytes() == b""
    assert _mode(empty) == 0o600
    assert _stages(tmp_path) == []


def test_symlink_target_is_replaced_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(OLD)
    work = tmp_path / "work"
    work.mkdir()
    leaf = work / "payload.json"
    leaf.symlink_to(victim)
    _write(LocalFileAccess(), leaf, b"secret")
    assert victim.read_bytes() == OLD
    assert not leaf.is_symlink()
    assert leaf.is_file()
    assert _mode(leaf) == 0o600
    assert leaf.read_bytes() == b"secret"


F_STEPS = ["begin", "mkstemp", *STEPS[:5], "close", "replace", "abort"]


@pytest.mark.parametrize("step", F_STEPS)
def test_error_text_is_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    step: str,
) -> None:
    work = tmp_path / f"dir_{MARKER}"
    work.mkdir()
    target = work / f"{MARKER}.json"
    marked = str(target)
    recorded: list[int] = []
    if step == "begin":

        def mkdir(self: Path, *a: Any, **k: Any) -> None:
            raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(self))

        monkeypatch.setattr(Path, "mkdir", mkdir)
        code = errno.EACCES
    elif step == "mkstemp":

        def mkstemp(*_a: Any, **k: Any) -> Any:
            raise OSError(errno.EACCES, os.strerror(errno.EACCES), str(k.get("dir", marked)))

        monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
        code = errno.EACCES
    elif step == "abort":
        real_abort = LocalOutputTxn.abort

        def abort(self: LocalOutputTxn) -> None:
            real_abort(self)
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), marked)

        monkeypatch.setattr(LocalOutputTxn, "abort", abort)
        name, fake, code = _fault("write", recorded, marked)
        monkeypatch.setattr(os, name, fake)
    else:
        name, fake, code = _fault(step, recorded, marked)
        monkeypatch.setattr(os, name, fake)
    caplog.set_level(logging.DEBUG)
    try:
        with pytest.raises(BaseException) as info:
            _write(LocalFileAccess(), target, f"payload {MARKER}".encode(), key=f"k.{MARKER}")
    finally:
        monkeypatch.undo()
    exc = info.value
    assert isinstance(exc, OSError), type(exc).__name__
    for text in (str(exc), repr(exc)):
        # "Input/output error" is EIO's own text; any other slash is a leaked path
        assert MARKER not in text and "/" not in text.replace(os.strerror(code), ""), text
    assert exc.filename is None
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
    assert exc.errno == code
    for record in caplog.records:
        assert MARKER not in record.getMessage()


def test_strict_policy_private_label_is_0600(tmp_path: Path) -> None:
    strict = RootRelative(tmp_path)
    try:
        with _umask(0):
            txn = strict.output_dir(_req(tmp_path / "payload.json")).begin(
                "file", "replace", label="isolated"
            )
            fd = txn.create_file("payload.json")
            try:
                mode = stat.S_IMODE(os.fstat(fd).st_mode)
            finally:
                os.close(fd)
                txn.abort()
    finally:
        strict.close()
    assert mode == 0o600


def test_ambient_isolated_label_is_private(tmp_path: Path) -> None:
    with _umask(0):
        txn = (
            LocalFileAccess()
            .output_dir(_req(tmp_path / "payload.json"))
            .begin("file", "replace", label="isolated")
        )
        fd = txn.create_file("payload.json")
        try:
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
            txn.abort()
    assert mode == 0o600

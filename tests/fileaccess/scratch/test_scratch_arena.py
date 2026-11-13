"""The scratch arena (F25 S1a): modes under any umask, descriptor checks, release rule, exits."""

from __future__ import annotations

import errno
import fcntl
import gc
import os
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess, reasons
from decoy_engine.errors import DecoyError
from decoy_engine.fileaccess import (
    CleanupReport,
    FileAccessDenied,
    FileAccessUnavailable,
    FileRole,
    ProtectedDirUnsafe,
    QuotaReport,
    Root,
    RootedFileAccess,
    ScratchArena,
    open_protected_dir,
)
from decoy_engine.fileaccess._scratch_reports import Disposition
from decoy_engine.fileaccess._types import StageDir
from decoy_engine.fileaccess.adapters import arrow, serial
from tests.fileaccess.support import ALL_ACCESS

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="the win32 arena is S1w"),
    pytest.mark.serial,  # os.umask is process-wide
]
Strict = Callable[[], AbstractContextManager[object]]
SENTINELS = ("/srv/secret-path", "patients_tbl", "ssn_col", "123-45-6789")


@pytest.fixture(autouse=True)
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    assert os.geteuid() != 0, "root ignores modes, so these cells would prove nothing"
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.fixture
def strict(tmp_path: Path) -> Iterator[Strict]:
    fd = os.open(tmp_path, os.O_RDONLY)
    roots = [Root("tmp", fd, frozenset(FileRole), ALL_ACCESS)]
    policy = RootedFileAccess(roots, allow_portable_fallback=True)
    yield lambda: fileaccess.scope(policy)
    os.close(fd)


@pytest.fixture
def umask() -> Iterator[Callable[[int], int]]:
    old = os.umask(0o022)
    yield os.umask
    os.umask(old)


def _mode(path: str | Path) -> int:
    found = os.lstat(path)
    assert found.st_uid == os.geteuid()
    return stat.S_IMODE(found.st_mode)


def _assert_marker(arena: ScratchArena) -> None:
    """A fresh read-only open of the marker by dir_fd: regular, euid, 0600, the name's token."""
    name = os.path.basename(arena.local_path())
    root = os.open(arena.local_path(), os.O_RDONLY | os.O_DIRECTORY)
    fd = os.open(".decoy-arena", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
    found, data = os.fstat(fd), os.read(fd, 64)
    os.close(fd)
    os.close(root)
    assert stat.S_ISREG(found.st_mode) and found.st_uid == os.geteuid()
    assert stat.S_IMODE(found.st_mode) == 0o600 and len(name) == len("decoy-arena-v1-") + 16
    assert data == b"decoy-arena-v1:" + name.removeprefix("decoy-arena-v1-").encode() + b"\n"


@pytest.mark.parametrize("mask", [0o000, 0o002, 0o022, 0o077, 0o777], ids="{:03o}".format)
def test_modes_under_umask(tmp_path: Path, umask: Callable[[int], int], mask: int) -> None:
    umask(mask)
    with ScratchArena.open() as arena, ScratchArena.open(under=tmp_path / "a" / "b") as under:
        child = arena.create_dir("c")
        os.close(child.create_file("f"))
        os.close(arena.create_file("g"))
        with ScratchArena._adopt(tmp_path / "x" / "y") as adopted:
            os.close(adopted.create_file("h"))
        dirs: list[str | Path] = [
            arena.local_path(),
            child.local_path(),
            under.local_path(),
            tmp_path / "a",
        ]
        dirs += [tmp_path / "a" / "b", tmp_path / "x", tmp_path / "x" / "y"]
        assert [_mode(d) for d in dirs] == [0o700] * len(dirs)
        files: list[str | Path] = [
            child.local_path("f"),
            arena.local_path("g"),
            tmp_path / "x" / "y" / "h",
        ]
        assert [_mode(f) for f in files] == [0o600] * len(files)
        _assert_marker(arena)
        _assert_marker(under)


def _reporting(mode: int, real: Callable[[int], os.stat_result] = os.fstat) -> object:
    return lambda fd: os.stat_result((stat.S_IFMT(real(fd).st_mode) | mode, *tuple(real(fd))[1:10]))


def test_arena_refuses_a_mode_it_cannot_set(
    temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ScratchArena.open() as arena:
        os.close(arena.create_file("kept"))
        arena.create_dir("kept_dir")
        with monkeypatch.context() as patch:
            patch.setattr(os, "fstat", _reporting(0o777))
            for make in (arena.create_dir, arena.create_file):
                with pytest.raises(ProtectedDirUnsafe):
                    make("new")
                assert not os.path.lexists(arena.local_path("new"))
            with pytest.raises(FileExistsError):
                arena.create_dir("kept_dir")
        assert [_mode(arena.local_path(n)) for n in ("kept", "kept_dir")] == [0o600, 0o700]
    monkeypatch.setattr(os, "mkdir", Mock(side_effect=OSError(errno.ENOSPC, "no space")))
    with pytest.raises(OSError) as info:
        ScratchArena.open()
    assert info.value.errno == errno.ENOSPC and not isinstance(info.value, ProtectedDirUnsafe)


def test_arena_root_is_an_issued_root(strict: Strict) -> None:
    with strict():
        arena = ScratchArena.open()
        inside = arena.local_path("x")
        assert open_protected_dir(inside, create=True) == Path(inside)
        assert arena.reclaim().disposition is Disposition.REMOVED
        with pytest.raises(FileAccessUnavailable):
            open_protected_dir(inside, create=True)


@pytest.mark.parametrize("cell", ["000", "022", "077", "777", "777_wrong_mode"])
def test_marker_mode_under_umask(
    temp_root: Path, umask: Callable[[int], int], monkeypatch: pytest.MonkeyPatch, cell: str
) -> None:
    umask(int(cell[:3], 8))
    if cell.endswith("wrong_mode"):
        monkeypatch.setattr(os, "fchmod", lambda fd, _m, real=os.fchmod: real(fd, 0o644))
        with pytest.raises(ProtectedDirUnsafe):
            ScratchArena.open()
        assert list(temp_root.iterdir()) == []  # no final-name arena, no pending directory
        return
    with ScratchArena.open() as arena:
        _assert_marker(arena)


@pytest.mark.parametrize("cell", ["symlink", "wrong_mode", "owner_bits_missing", "not_dir"])
def test_refuses_unsafe_root(temp_root: Path, monkeypatch: pytest.MonkeyPatch, cell: str) -> None:
    planted = temp_root / "decoy-arena-pending-planted"
    if cell == "symlink":
        planted.symlink_to(temp_root.parent)
    elif cell in ("wrong_mode", "owner_bits_missing"):
        planted.mkdir()
        planted.chmod(0o755 if cell == "wrong_mode" else 0o500)  # 0500: only the exact mode fails
    else:
        planted.touch()
    before = sorted((p.name, os.lstat(p).st_mode) for p in temp_root.iterdir())
    monkeypatch.setattr(fileaccess._scratch, "_allocate", lambda base, prefix: str(planted))
    with pytest.raises(ProtectedDirUnsafe) as info:
        ScratchArena.open()
    assert info.value.code == reasons.runtime.SCRATCH_UNSAFE
    assert sorted((p.name, os.lstat(p).st_mode) for p in temp_root.iterdir()) == before


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.mark.parametrize("cell", ["symlinked_under", "base_open_fails", "check_fails"])
def test_open_leaves_nothing_behind(
    temp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cell: str
) -> None:
    before = _open_fds()
    if cell == "symlinked_under":
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "link").symlink_to(real)
        with ScratchArena.open(under=tmp_path / "link") as arena:
            os.close(arena.create_file("f"))
        assert list(real.iterdir()) == []
    elif cell == "base_open_fails":
        real_open = os.open

        def refuse_base(path: str, flags: int, *args: Any, **kwargs: Any) -> int:
            if kwargs.get("dir_fd") is None and os.fspath(path) == str(temp_root):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", refuse_base)
        with pytest.raises(PermissionError):
            ScratchArena.open()
        assert list(temp_root.iterdir()) == []
    else:
        failure = OSError(errno.EIO, os.strerror(errno.EIO))
        monkeypatch.setattr(fileaccess._scratch, "_exact", Mock(side_effect=failure))
        with pytest.raises(OSError) as info:
            ScratchArena.open()
        assert info.value.errno == errno.EIO
        assert [p for p in temp_root.iterdir() if p.name.startswith("decoy-arena-v1-")] == []
    assert _open_fds() == before


def test_a_child_keeps_its_opener_alive(temp_root: Path) -> None:
    child = ScratchArena.open().create_dir("d")
    gc.collect()
    os.close(child.create_dir("e").create_file("f"))
    assert os.path.isfile(os.path.join(child.local_path("e"), "f"))
    del child
    gc.collect()
    assert [p for p in temp_root.iterdir() if p.name.startswith("decoy-arena-")] == []


def test_remove_takes_a_file_or_an_empty_directory() -> None:
    with ScratchArena.open() as arena:
        os.close(arena.create_file("f"))
        arena.create_dir("d")
        os.close(arena.create_dir("full").create_file("x"))
        arena.remove("f")
        arena.remove("d")
        with pytest.raises(OSError) as info:
            arena.remove("full")
        assert info.value.errno in (errno.ENOTEMPTY, errno.EEXIST)
        assert sorted(os.listdir(arena.local_path())) == [".decoy-arena", "full"]


_EXITS = ["reclaim_twice", "error_in_with", "failed_reclaim", "collection", "adopted", "kill"]


@pytest.mark.parametrize("cell", _EXITS)
def test_exit_paths(temp_root: Path, tmp_path: Path, cell: str) -> None:
    arena = ScratchArena.open()
    if cell == "reclaim_twice":
        os.close(arena.create_dir("d").create_file("f"))
        first = arena.reclaim()
        assert first == CleanupReport(disposition=Disposition.REMOVED)
        assert arena.reclaim() is first
        with pytest.raises(FileAccessUnavailable):
            arena.create_file("late")
    elif cell == "error_in_with":
        with pytest.raises(KeyError, match="primary"), arena:
            raise KeyError("primary")
    elif cell == "failed_reclaim":
        sub = arena.create_dir("d")
        os.close(sub.create_file("f"))
        stuck = Path(sub.local_path())
        stuck.chmod(0o500)  # the unlink inside fails EACCES, so reclaim cannot finish
        with pytest.raises(KeyError, match="primary"), arena:
            raise KeyError("primary")
        assert arena.reclaim().disposition is Disposition.RETAINED and stuck.exists()
        stuck.chmod(0o700)
        return
    elif cell == "collection":
        child = arena.create_dir("c")
        child_path = child.local_path()
        with pytest.raises(FileAccessUnavailable):
            child.reclaim()
        del child
        gc.collect()
        assert os.path.isdir(child_path)  # a child closes only its descriptor
        del arena
        gc.collect()
    elif cell == "adopted":
        arena.reclaim()
        with ScratchArena._adopt(tmp_path / "adopted") as adopted:
            with pytest.raises(FileAccessUnavailable):
                adopted.reclaim()
        assert (tmp_path / "adopted").is_dir()
    else:
        arena.reclaim()
        code = "from decoy_engine.fileaccess import ScratchArena as A; a = A.open(); "
        code += "print(a.local_path(), flush=True); input()"
        env = {**os.environ, "TMPDIR": str(temp_root)}
        argv, pipe = [sys.executable, "-c", code], subprocess.PIPE
        with subprocess.Popen(argv, stdin=pipe, stdout=pipe, env=env) as proc:  # noqa: S603
            assert proc.stdout is not None
            path = proc.stdout.readline().decode().strip()
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the live owner holds LOCK_SH
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.close(fd)
            assert os.path.isfile(os.path.join(path, ".decoy-arena"))
        return
    assert [p for p in temp_root.iterdir() if p.name.startswith("decoy-arena-")] == []


def test_arena_is_stagedir(umask: Callable[[int], int]) -> None:
    umask(0o000)
    with ScratchArena.open() as arena:
        assert isinstance(arena, StageDir) and arena.strict and arena.display == "engine.scratch"
        schema = pa.schema([("a", pa.int64())])
        with arrow.parquet_writer(arena, "t.parquet", schema) as writer:
            writer.write_table(pa.table({"a": [1]}))
        serial.write_text(arena, "t.txt", "x")
        assert [_mode(arena.local_path(n)) for n in ("t.parquet", "t.txt")] == [0o600] * 2
        os.close(arena.create_file("keep"))
        refused = _raised(
            lambda: arena.create_file(".decoy-arena"),
            lambda: arena.create_dir(".decoy-arena"),
            lambda: arena.remove(".decoy-arena"),
            lambda: arena.rename(".decoy-arena", "x"),
            lambda: arena.rename("keep", ".decoy-arena"),
        )
        assert all(isinstance(e, FileAccessDenied) for e in refused)
        _assert_marker(arena)
        with pytest.raises(FileAccessUnavailable):
            arena.local_stage_path()


def test_local_path_policy(tmp_path: Path, strict: Strict) -> None:
    with ScratchArena.open() as engine, ScratchArena.open(under=tmp_path / "u") as caller:
        name = os.path.basename(caller.local_path())
        assert caller.local_path("x") == str(tmp_path / "u" / name / "x")
        assert engine.local_path().startswith(tempfile.gettempdir())
        with strict():
            assert engine.local_path("x") == os.path.join(engine.local_path(), "x")
            with pytest.raises(FileAccessUnavailable):
                ScratchArena.open(under=tmp_path / "v")
            with ScratchArena.open(under=engine.local_path()) as strict_under:
                with pytest.raises(FileAccessUnavailable):
                    strict_under.local_path()


def _raised(*calls: Callable[[], object]) -> list[BaseException]:
    out: list[BaseException] = []
    for call in calls:
        with pytest.raises((DecoyError, OSError, ValueError)) as info:
            call()
        out.append(info.value)
    return out


@pytest.mark.parametrize("cell", ["reports", "errors"])
def test_report_text_is_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: Strict, cell: str
) -> None:
    secret = tmp_path / "srv" / "secret-path" / "patients_tbl"
    texts: list[object] = []
    if cell == "reports":
        texts += [
            QuotaReport(dimension="bytes", effective_bytes_cap=10, observed_bytes=11),
            QuotaReport(dimension="unknown", route="out_of_core"),
            CleanupReport(disposition=Disposition.RETAINED, retained_bytes=None),
        ]
        texts += _raised(  # a sentinel offered as a code or count is refused, never stored
            lambda: QuotaReport(dimension="bytes", route=SENTINELS[0]),  # type: ignore[arg-type]
            lambda: QuotaReport(dimension=SENTINELS[1]),  # type: ignore[arg-type]
            lambda: CleanupReport(disposition=SENTINELS[2]),  # type: ignore[arg-type]
            lambda: CleanupReport(disposition=Disposition.REMOVED, retained_bytes=SENTINELS[3]),  # type: ignore[arg-type]
        )
    else:
        with ScratchArena.open(under=secret) as arena:
            texts += _raised(
                lambda: arena.create_file("ssn_col/123-45-6789"),
                lambda: arena.create_file(".decoy-arena"),
                lambda: arena.open_file("patients_tbl"),
                lambda: arena.remove("ssn_col"),
            )
            with monkeypatch.context() as patch:
                patch.setattr(os, "fstat", _reporting(0o777))
                texts += _raised(lambda: arena.create_dir("patients_tbl"))
            with strict(), ScratchArena.open(under=arena.local_path()) as inner:
                texts += _raised(lambda: ScratchArena.open(under=secret), inner.local_path)
        texts += _raised(lambda: arena.local_path("ssn_col"))
        names = "Denied Denied FileNotFound FileNotFound ProtectedDirUnsafe" + " Unavailable" * 3
        assert [
            type(e).__name__.removeprefix("FileAccess").removesuffix("Error") for e in texts
        ] == names.split()
    for each in texts:
        assert not any(s in f"{each!s} {each!r}" for s in SENTINELS), repr(each)
        if isinstance(each, BaseException):
            assert each.__context__ is None and each.__cause__ is None


# -- the transitional Path arm under a strict policy, and the arena's reader (S2a-1b-i) --------


@pytest.mark.parametrize("cell", ["under_live_root", "outside_refused", "dotdot_refused"])
def test_adopt_under_a_strict_policy(tmp_path: Path, strict: Strict, cell: str) -> None:
    """A path at or below a live issued root is adopted under every policy, by the rule
    `open_protected_dir` applies; any other path still needs the ambient policy."""
    with ScratchArena.open() as arena:
        inside = Path(arena.local_path()) / "staged" / "t0000"
        with strict():
            if cell == "under_live_root":
                with ScratchArena._adopt(inside) as adopted:
                    os.close(adopted.create_file("masked_keys.parquet"))
                assert _mode(inside.parent) == _mode(inside) == 0o700
                assert (inside / "masked_keys.parquet").is_file()
            elif cell == "outside_refused":
                outside = tmp_path / "outside" / "x"
                with pytest.raises(FileAccessUnavailable):
                    ScratchArena._adopt(outside)
                assert not (tmp_path / "outside").exists(), "refused before anything is made"
            else:  # `..` that stays inside is refused by `_vet_live`; one that leaves is outside
                within = f"{arena.local_path()}/staged/../staged/t0000"
                with pytest.raises(ProtectedDirUnsafe):
                    ScratchArena._adopt(within)
                assert not inside.parent.exists()
                escape = f"{arena.local_path()}/staged/../../escape"
                with pytest.raises(FileAccessUnavailable):
                    ScratchArena._adopt(escape)
                assert not os.path.exists(os.path.normpath(escape))
    if cell == "under_live_root":
        with strict(), pytest.raises(FileAccessUnavailable):
            ScratchArena._adopt(inside)  # the root is no longer live once reclaimed


def _staged(arena: ScratchArena, name: str) -> pa.Table:
    table = pa.table({"k": [1, 2, 3]})
    with arrow.parquet_writer(arena, name, table.schema) as writer:
        writer.write_table(table)
    return table


def test_reader_reads_only_beneath_its_directory(tmp_path: Path, strict: Strict) -> None:
    from decoy_engine.profile._readers import LazySource

    outside = tmp_path / "outside.parquet"
    pq.write_table(pa.table({"k": [9]}), outside)
    with ScratchArena.open() as arena:
        os.close(arena.create_file("top.parquet"))
        child = arena.create_dir("staged")
        table = _staged(child, "k.parquet")
        reader = child.reader()
        released = Path(child.local_path("k.parquet"))
        for scope in (nullcontext, strict):  # the reader is its own policy, whatever is ambient
            with scope():
                # T02 holds the bare component; T18 the absolute released path (prefix stripped).
                assert LazySource(Path("k.parquet"), access=reader).to_table() == table
                assert LazySource(released, access=reader).to_table() == table
                for denied in (outside, Path("../top.parquet"), released.parent / ".." / "x"):
                    with pytest.raises(FileAccessDenied):
                        LazySource(denied, access=reader).to_table()
    with pytest.raises(FileAccessUnavailable):
        arena.reader()  # reclaimed


def test_reader_holds_its_own_descriptor() -> None:
    """The reader outlives the arena object it came from (its own descriptor), never the disk."""
    from decoy_engine.profile._readers import LazySource

    with ScratchArena.open() as arena:
        child = arena.create_dir("staged")
        table = _staged(child, "k.parquet")
        reader = child.reader()
        del child
        gc.collect()  # the child's finalizer closes the child's descriptor
        assert LazySource(Path("k.parquet"), access=reader).to_table() == table
    with pytest.raises(FileAccessDenied):  # reclaimed: the file is gone beneath the descriptor
        LazySource(Path("k.parquet"), access=reader).to_table()

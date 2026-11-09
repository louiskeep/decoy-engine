"""A-4's named creators: modes are set by the creator under any umask, errno classes stay."""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from decoy_engine.fileaccess import FileAccessDenied, RootedFileAccess
from decoy_engine.fileaccess._output import (
    create_output_file_at,
    create_private_at,
    create_private_child_at,
    make_stage_dir_at,
)
from decoy_engine.fileaccess._types import FileRequest, FileRole

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="descriptor creators are POSIX"),
    pytest.mark.serial,  # os.umask is process-wide
]


@pytest.fixture(autouse=True)
def _posix_preconditions() -> None:
    assert os.geteuid() != 0, "root ignores modes, so these cells would prove nothing"
    assert os.chmod in os.supports_dir_fd


@pytest.fixture
def umask() -> Iterator[Callable[[int], int]]:
    old = os.umask(0o022)
    yield os.umask
    os.umask(old)


@pytest.fixture
def dir_fd(tmp_path: Path) -> Iterator[int]:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    yield fd
    os.close(fd)


def _mode(fd: int) -> int:
    return stat.S_IMODE(os.fstat(fd).st_mode)


def _lmode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o077, 0o200, 0o777])
def test_make_stage_dir_at_under_umask(
    tmp_path: Path, dir_fd: int, umask: Callable[[int], int], mask: int
) -> None:
    umask(mask)
    fd = make_stage_dir_at(dir_fd, "x")
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_uid == os.geteuid()
        assert _mode(fd) == 0o700 and _lmode(tmp_path / "x") == 0o700
    finally:
        os.close(fd)


def test_make_stage_dir_at_refuses_a_symlink_at_the_name(tmp_path: Path, dir_fd: int) -> None:
    target = tmp_path / "target"
    target.mkdir()
    os.chmod(target, 0o755)  # noqa: S103 - the test needs an open mode
    os.symlink(target, tmp_path / "link")
    with pytest.raises(FileExistsError):
        make_stage_dir_at(dir_fd, "link")
    assert _lmode(target) == 0o755 and os.path.islink(tmp_path / "link")


def test_make_stage_dir_at_open_does_not_follow_a_swapped_in_symlink(
    tmp_path: Path, dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    os.chmod(target, 0o755)  # noqa: S103 - the test needs an open mode
    real_mkdir = os.mkdir

    def swapped(name: str, mode: int = 0o777, *, dir_fd: int) -> None:
        real_mkdir(name, mode, dir_fd=dir_fd)
        os.rmdir(name, dir_fd=dir_fd)
        os.symlink(target, name, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swapped)
    with pytest.raises(OSError):
        make_stage_dir_at(dir_fd, "x")
    assert _lmode(target) == 0o755 and os.path.islink(tmp_path / "x")


def test_make_stage_dir_at_eexist_leaves_existing(tmp_path: Path, dir_fd: int) -> None:
    existing = tmp_path / "x"
    existing.mkdir()  # empty, so a stray rmdir after EEXIST would succeed
    before = os.lstat(existing).st_ino
    os.chmod(existing, 0o755)  # noqa: S103 - the test needs an open mode
    with pytest.raises(FileExistsError):
        make_stage_dir_at(dir_fd, "x")
    assert existing.is_dir() and os.lstat(existing).st_ino == before
    assert _lmode(existing) == 0o755


def test_make_stage_dir_at_enospc_keeps_errno(dir_fd: int, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(*_a: object, **_k: object) -> None:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(os, "mkdir", full)
    with pytest.raises(OSError) as info:
        make_stage_dir_at(dir_fd, "x")
    assert info.value.errno == errno.ENOSPC and "x" not in str(info.value)


def _always(fd: int, mask: int) -> None:
    made = create_private_at(fd, "f")
    try:
        assert stat.S_ISREG(os.fstat(made).st_mode) and os.fstat(made).st_uid == os.geteuid()
        assert _mode(made) == 0o600
    finally:
        os.close(made)


def _child(fd: int, mask: int) -> None:
    made = create_private_child_at(fd, "f")
    try:
        assert stat.S_ISREG(os.fstat(made).st_mode) and _mode(made) == 0o600
    finally:
        os.close(made)


def _always_fchmod_even_at_0600(fd: int, mask: int) -> None:
    calls: list[int] = []
    real = os.fchmod

    def spy(target: int, mode: int) -> None:
        calls.append(mode)
        real(target, mode)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fchmod", spy)
        os.close(create_private_at(fd, "f"))  # born 0600 under umask 000: still chmod'ed
    assert calls == [0o600]


def _child_mode_emulating(fd: int, mask: int) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError(errno.EPERM, "no chmod here")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o777))
        patch.setattr(os, "fchmod", refuse)
        os.close(create_private_child_at(fd, "f"))  # owner bits present: no chmod attempted


_CELLS = {
    "always_000": (_always, 0o000),
    "always_022": (_always, 0o022),
    "always_777": (_always, 0o777),
    "child_000": (_child, 0o000),
    "child_022": (_child, 0o022),
    "child_777": (_child, 0o777),
    "child_mode_emulating": (_child_mode_emulating, 0o022),
    "always_fchmod_at_0600": (_always_fchmod_even_at_0600, 0o000),
}


@pytest.mark.parametrize("cell", list(_CELLS))
def test_private_entry_points(dir_fd: int, umask: Callable[[int], int], cell: str) -> None:
    check, mask = _CELLS[cell]
    umask(mask)
    check(dir_fd, mask)


@pytest.mark.parametrize("creator", [create_private_at, create_private_child_at])
def test_private_entry_points_are_exclusive_and_do_not_touch_an_existing_file(
    tmp_path: Path, dir_fd: int, creator: Callable[[int, str], int]
) -> None:
    existing = tmp_path / "f"
    existing.write_text("keep")
    os.chmod(existing, 0o644)
    with pytest.raises(FileExistsError):
        creator(dir_fd, "f")
    assert existing.read_text() == "keep" and _lmode(existing) == 0o644


def test_private_at_fchmod_failure_closes_and_unlinks_what_it_created(
    tmp_path: Path, dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[int] = []
    real_open = os.open

    def tracking(path: str, flags: int, mode: int = 0o777, **kwargs: int) -> int:
        fd = real_open(path, flags, mode, **kwargs)  # type: ignore[arg-type]
        opened.append(fd)
        return fd

    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError(errno.EPERM, "no chmod")

    monkeypatch.setattr(os, "open", tracking)
    monkeypatch.setattr(os, "fchmod", refuse)
    with pytest.raises(PermissionError):
        create_private_at(dir_fd, "f")
    monkeypatch.undo()
    assert not (tmp_path / "f").exists()
    with pytest.raises(OSError):
        os.fstat(opened[-1])  # the descriptor was closed


@pytest.mark.parametrize("bad", ["../x", "/abs/x", "a/b", "..", ".", "", "a\0b"])
@pytest.mark.parametrize(
    "creator",
    [
        make_stage_dir_at,
        create_private_at,
        create_private_child_at,
        lambda fd, name: create_output_file_at(fd, name, 0o666),
    ],
    ids=["stage_dir", "private", "private_child", "output_file"],
)
def test_creators_refuse_a_bad_name_before_creating_anything(
    tmp_path: Path, dir_fd: int, creator: Callable[[int, str], int], bad: str
) -> None:
    sibling = tmp_path.parent / "x"
    with pytest.raises(FileAccessDenied):
        creator(dir_fd, bad)
    assert list(tmp_path.iterdir()) == [] and not sibling.exists()


# -- A-4 stages keep their behaviour through the named creators ---------------------------------


def _begin(rooted: RootedFileAccess, kind: str, label: str, locator: str = "out/leaf"):  # type: ignore[no-untyped-def]
    out = rooted.output_dir(FileRequest(FileRole.TARGET_OUTPUT, "write", locator, "targets.t.path"))
    return out.begin(kind, "exclusive", label=label)  # type: ignore[arg-type]


def _stage(root_dir: Path) -> Path:
    (found,) = [p for p in (root_dir / "out").iterdir() if p.name.startswith("_decoy_")]
    return found


@pytest.fixture
def prepared(root_dir: Path) -> Path:
    (root_dir / "out").mkdir()
    return root_dir


def _refuse(*_a: object, **_k: object) -> None:
    raise PermissionError(errno.EPERM, "no chmod")


def _private_file_stage_777(rooted: RootedFileAccess, root: Path) -> None:
    txn = _begin(rooted, "file", "isolated")
    os.close(txn.create_file("x"))
    assert _lmode(_stage(root)) == 0o600
    txn.publish()
    assert _lmode(root / "out" / "leaf") == 0o600


def _private_dir_child_777(rooted: RootedFileAccess, root: Path) -> None:
    txn = _begin(rooted, "dir", "isolated")
    os.close(txn.create_file("c"))
    assert _lmode(_stage(root) / "c") == 0o600
    txn.publish()
    assert _lmode(root / "out" / "leaf" / "c") == 0o600


def _dir_stage_777(rooted: RootedFileAccess, root: Path) -> None:
    txn = _begin(rooted, "dir", "table_sink")
    assert _lmode(_stage(root)) == 0o700
    txn.publish()
    assert _lmode(root / "out" / "leaf") == 0o700


def _private_top_fchmod_error_unchanged(rooted: RootedFileAccess, root: Path) -> None:
    txn = _begin(rooted, "file", "isolated")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fchmod", _refuse)
        with pytest.raises(PermissionError):
            txn.create_file("x")
    txn.abort()
    assert list((root / "out").iterdir()) == []


def _mode_emulating_private_child(rooted: RootedFileAccess, root: Path) -> None:
    txn = _begin(rooted, "dir", "isolated")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o777))
        patch.setattr(os, "fchmod", _refuse)
        os.close(txn.create_file("c"))
    txn.publish()
    assert (root / "out" / "leaf" / "c").exists()


def _public_dir_stage_unchanged(rooted: RootedFileAccess, root: Path) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "chmod", _refuse)
        patch.setattr(os, "fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFDIR | 0o777))
        txn = _begin(rooted, "dir", "table_sink")
    assert _lmode(_stage(root)) == 0o700
    txn.publish()
    assert (root / "out" / "leaf").is_dir()


def _public_file_mode_unchanged(rooted: RootedFileAccess, root: Path) -> None:
    txn = _begin(rooted, "file", "table_sink")
    os.close(txn.create_file("x"))
    assert _lmode(_stage(root)) == 0o644
    txn.publish()


def _output_parent_unchanged(rooted: RootedFileAccess, root: Path) -> None:
    txn = _begin(rooted, "file", "table_sink", "fresh/leaf")
    assert _lmode(root / "fresh") == 0o755
    txn.abort()


_STAGE_CELLS = {
    "private_file_stage_777": (_private_file_stage_777, 0o777),
    "private_top_fchmod_error_unchanged": (_private_top_fchmod_error_unchanged, 0o022),
    "private_dir_child_777": (_private_dir_child_777, 0o777),
    "mode_emulating_private_child": (_mode_emulating_private_child, 0o022),
    "dir_stage_777": (_dir_stage_777, 0o777),
    "public_dir_stage_unchanged": (_public_dir_stage_unchanged, 0o022),
    "public_file_mode_unchanged": (_public_file_mode_unchanged, 0o022),
    "output_parent_unchanged": (_output_parent_unchanged, 0o022),
}


@pytest.mark.parametrize("cell", list(_STAGE_CELLS))
def test_a4_stages_keep_behaviour(
    rooted: RootedFileAccess, prepared: Path, umask: Callable[[int], int], cell: str
) -> None:
    check, mask = _STAGE_CELLS[cell]
    umask(mask)
    check(rooted, prepared)

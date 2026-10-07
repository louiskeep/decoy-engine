"""AT-E5 and AT-E12: `RootedFileAccess` and `DirHandle` confinement, and directory capabilities."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import pytest

from decoy_engine.fileaccess import (
    FileAccessDenied,
    FileRequest,
    FileRole,
    RootedFileAccess,
)

SENTINEL = b"SENTINEL-CONTENT"


def _req(locator: str, role: FileRole = FileRole.SOURCE, access: str = "read") -> FileRequest:
    return FileRequest(role, access, locator, "sources.t.path")  # type: ignore[arg-type]


def _assert_denied(policy: RootedFileAccess, locator: str, **kw: object) -> None:
    with pytest.raises(FileAccessDenied) as info:
        policy.open_read(_req(locator, **kw))  # type: ignore[arg-type]
    assert str(info.value) == "file access denied: sources.t.path"
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_reads_a_regular_file_beneath_the_root(rooted: RootedFileAccess, root_dir: Path) -> None:
    (root_dir / "d").mkdir()
    (root_dir / "d" / "a.bin").write_bytes(SENTINEL)
    with rooted.open_read(_req("d/a.bin")) as handle:
        assert handle.read_all() == SENTINEL
        assert handle.read_all(4) == SENTINEL[:4]
        assert Path(handle.library_path()).read_bytes() == SENTINEL
        assert handle.stat().st_size == len(SENTINEL)
        with handle.open_binary() as fh:
            assert fh.read(3) == SENTINEL[:3]
            fh.seek(0)
            assert fh.read() == SENTINEL
        assert handle.display == "sources.t.path"


def test_stays_readable_after_the_name_is_unlinked(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    (root_dir / "a.bin").write_bytes(SENTINEL)
    handle = rooted.open_read(_req("a.bin"))
    (root_dir / "a.bin").unlink()
    assert Path(handle.library_path()).read_bytes() == SENTINEL
    handle.close()


@pytest.mark.parametrize(
    "locator",
    [
        "../outside",
        "/etc/passwd",
        "a/../../outside",
        "",
        "x\0y",
        "missing.bin",
        "link_file",
        "link_dir/a.bin",
        "dir",
    ],
)
def test_every_escape_missing_or_non_regular_is_the_one_denial(
    rooted: RootedFileAccess, root_dir: Path, tmp_path: Path, locator: str
) -> None:
    (tmp_path / "outside").write_bytes(SENTINEL)
    (root_dir / "real.bin").write_bytes(SENTINEL)
    (root_dir / "dir").mkdir()
    (root_dir / "dir" / "a.bin").write_bytes(SENTINEL)
    (root_dir / "link_file").symlink_to(root_dir / "real.bin")
    (root_dir / "link_dir").symlink_to(root_dir / "dir")
    _assert_denied(rooted, locator)


def test_magic_link_inside_the_root_is_denied(rooted: RootedFileAccess, root_dir: Path) -> None:
    (root_dir / "real.bin").write_bytes(SENTINEL)
    keep = os.open(root_dir / "real.bin", os.O_RDONLY)
    try:
        os.symlink(f"/proc/self/fd/{keep}", root_dir / "magic")
        _assert_denied(rooted, "magic")
    finally:
        os.close(keep)


def test_fifo_and_device_are_denied(rooted: RootedFileAccess, root_dir: Path) -> None:
    os.mkfifo(root_dir / "pipe")
    _assert_denied(rooted, "pipe")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(root_dir / "s.sock"))
    sock.close()
    _assert_denied(rooted, "s.sock")


def test_a_role_or_access_the_root_does_not_serve_is_denied(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    (root_dir / "a.bin").write_bytes(SENTINEL)
    _assert_denied(rooted, "a.bin", role=FileRole.LOG_OUTPUT)
    _assert_denied(rooted, "a.bin", access="write")


def test_denylisted_inode_is_denied_even_beneath_the_root(
    root_dir: Path,
) -> None:
    from decoy_engine.fileaccess import Root

    (root_dir / "key").write_bytes(SENTINEL)
    st = (root_dir / "key").stat()
    fd = os.open(root_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        policy = RootedFileAccess(
            [Root("r", fd, frozenset({FileRole.SOURCE}))], deny_inodes=[(st.st_dev, st.st_ino)]
        )
        _assert_denied(policy, "key")
    finally:
        os.close(fd)


def test_rename_race_never_reads_outside_the_root(
    rooted: RootedFileAccess, root_dir: Path, tmp_path: Path
) -> None:
    (tmp_path / "outside").write_bytes(b"OUTSIDE")
    (root_dir / "leaf").write_bytes(SENTINEL)
    stop = threading.Event()

    def swap() -> None:
        while not stop.is_set():
            try:
                os.replace(root_dir / "leaf", root_dir / "held")
                os.symlink(tmp_path / "outside", root_dir / "leaf")
                os.unlink(root_dir / "leaf")
                os.replace(root_dir / "held", root_dir / "leaf")
            except OSError:
                pass

    thread = threading.Thread(target=swap)
    thread.start()
    try:
        for _ in range(1000):
            try:
                with rooted.open_read(_req("leaf")) as handle:
                    assert handle.read_all() in (SENTINEL,)
            except FileAccessDenied:
                pass
    finally:
        stop.set()
        thread.join()


def test_directory_capability_children_open_only_beneath_the_original_fd(
    rooted: RootedFileAccess, root_dir: Path, tmp_path: Path
) -> None:
    (root_dir / "providers").mkdir()
    (root_dir / "providers" / "a.txt").write_text("x")
    (root_dir / "providers" / "sub").mkdir()
    (root_dir / "providers" / "sub" / "b.txt").write_text("y")
    (root_dir / "providers" / "ln").symlink_to(root_dir / "providers" / "a.txt")
    (tmp_path / "outside").write_text("OUT")
    request = FileRequest(FileRole.CUSTOM_PROVIDER_ROOT, "read_dir", "providers", "providers")
    handle = rooted.open_dir(request)
    assert sorted(e.name for e in handle.entries()) == ["a.txt", "ln", "sub"]
    assert sorted(e.name for e in handle.entries()) == ["a.txt", "ln", "sub"]
    assert handle.open_file("a.txt").read_all() == b"x"
    assert handle.open_file("sub/b.txt").read_all() == b"y"
    sub = handle.open_dir("sub")
    assert [e.name for e in sub.entries()] == ["b.txt"]
    for bad in ("ln", "../../outside", "/abs", "..", "missing", "sub"):
        with pytest.raises(FileAccessDenied):
            handle.open_file(bad)
    os.rename(root_dir / "providers", root_dir / "moved")
    os.mkdir(root_dir / "providers")
    (root_dir / "providers" / "a.txt").write_text("PLANTED")
    assert handle.open_file("a.txt").read_all() == b"x"
    assert not hasattr(handle, "path")
    assert "providers" not in repr(handle) and str(root_dir) not in repr(handle)
    assert handle.display == "providers"


def test_directory_role_requires_a_directory_and_file_role_a_file(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    (root_dir / "f").write_text("x")
    (root_dir / "d").mkdir()
    with pytest.raises(FileAccessDenied):
        rooted.open_dir(FileRequest(FileRole.CUSTOM_PROVIDER_ROOT, "read_dir", "f", "k"))
    with pytest.raises(FileAccessDenied):
        rooted.open_read(_req("d"))


def test_stat_of_a_spill_hint_directory(rooted: RootedFileAccess, root_dir: Path) -> None:
    (root_dir / "out").mkdir()
    st = rooted.stat(FileRequest(FileRole.SPILL_HINT, "stat", "out", "targets.t.path"))
    assert st.st_dev == os.stat(root_dir).st_dev
    with pytest.raises(FileAccessDenied):
        rooted.stat(FileRequest(FileRole.SPILL_HINT, "stat", "../x", "targets.t.path"))

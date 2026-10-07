"""AT-E6 and AT-E14: output transactions and the stage capability."""

from __future__ import annotations

import errno
import os
import socket
from pathlib import Path

import pytest

from decoy_engine.fileaccess import (
    FileAccessDenied,
    FileAccessUnavailable,
    FileRequest,
    FileRole,
    RootedFileAccess,
)
from decoy_engine.fileaccess._output import RootedOutputTxn

BAD_NAMES = ["/abs/x", "/", "..", ".", "../outside", "a/b", "a/../b", "", "a\0b", "n" * 256]


def _out(
    policy: RootedFileAccess, locator: str = "out/leaf", role: FileRole = FileRole.TARGET_OUTPUT
):  # type: ignore[no-untyped-def]
    return policy.output_dir(FileRequest(role, "write", locator, "targets.t.path"))


def _put(txn: object, name: str, data: bytes = b"data") -> None:
    fd = txn.create_file(name)  # type: ignore[attr-defined]
    os.write(fd, data)
    os.close(fd)


def _inodes(root: Path) -> dict[str, tuple[int, int, int, int]]:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p.relative_to(root))] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    return out


def test_dir_transaction_publishes_atomically_into_an_absent_leaf(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    txn = _out(rooted).begin("dir", "exclusive", label="table_sink")
    _put(txn, "a.parquet")
    nested = txn.create_dir("sub")
    _put(nested, "b.parquet")
    assert not (root_dir / "out" / "leaf").exists()
    (stage,) = [p for p in (root_dir / "out").iterdir()]
    assert stage.name.startswith("_decoy_stage_")
    txn.publish()
    assert sorted(p.name for p in (root_dir / "out" / "leaf").rglob("*")) == [
        "a.parquet",
        "b.parquet",
        "sub",
    ]
    assert [p.name for p in (root_dir / "out").iterdir()] == ["leaf"]


def test_dir_transaction_publishes_into_an_empty_directory(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    (root_dir / "out" / "leaf").mkdir(parents=True)
    txn = _out(rooted).begin("dir", "exclusive", label="table_sink")
    _put(txn, "a.parquet")
    txn.publish()
    assert [p.name for p in (root_dir / "out" / "leaf").iterdir()] == ["a.parquet"]


def test_dir_transaction_into_a_non_empty_directory_fails_closed_and_keeps_the_stage(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    leaf = root_dir / "out" / "leaf"
    leaf.mkdir(parents=True)
    (leaf / "keep.txt").write_text("occupant")
    before = _inodes(leaf)
    txn = _out(rooted).begin("dir", "exclusive", label="table_sink")
    _put(txn, "a.parquet")
    with pytest.raises(OSError) as info:
        txn.publish()
    assert info.value.errno in (errno.ENOTEMPTY, errno.EEXIST)
    assert "_decoy_stage_" not in str(info.value) and "leaf" not in str(info.value)
    assert _inodes(leaf) == before
    stages = [p for p in (root_dir / "out").iterdir() if p.name.startswith("_decoy_stage_")]
    assert len(stages) == 1 and (stages[0] / "a.parquet").exists()
    txn.abort()
    assert [p.name for p in (root_dir / "out").iterdir()] == ["leaf"]
    assert _inodes(leaf) == before


def test_abort_removes_the_whole_stage_and_is_idempotent(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    txn = _out(rooted).begin("dir", "exclusive", label="table_sink")
    _put(txn, "a")
    _put(txn.create_dir("d"), "b")
    txn.abort()
    txn.abort()
    assert list((root_dir / "out").iterdir()) == []


def test_context_manager_aborts_unless_published(rooted: RootedFileAccess, root_dir: Path) -> None:
    with _out(rooted).begin("dir", "exclusive", label="table_sink") as txn:
        _put(txn, "a")
    assert list((root_dir / "out").iterdir()) == []
    with _out(rooted).begin("dir", "exclusive", label="table_sink") as txn:
        _put(txn, "a")
        txn.publish()
    assert (root_dir / "out" / "leaf" / "a").exists()


def test_publish_and_abort_close_every_descriptor_they_opened(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    def fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    out = _out(rooted)
    baseline = fds()
    for action in ("publish", "abort"):
        txn = out.begin("dir", "exclusive", label="table_sink")
        _put(txn, "a")
        _put(txn.create_dir("d"), "b")
        if action == "publish":
            txn.publish()
            (root_dir / "out" / "leaf").rename(root_dir / "out" / "gone")
        else:
            txn.abort()
    assert fds() == baseline


def test_a_collected_transaction_cleans_its_stage(rooted: RootedFileAccess, root_dir: Path) -> None:
    import gc

    txn = _out(rooted).begin("dir", "exclusive", label="table_sink")
    _put(txn, "a")
    del txn
    gc.collect()
    assert list((root_dir / "out").iterdir()) == []


def test_file_exclusive_refuses_an_existing_leaf_and_replace_swaps(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    (root_dir / "out").mkdir()
    leaf = root_dir / "out" / "leaf"
    leaf.write_text("occupant")
    txn = _out(rooted).begin("file", "exclusive", label="quarantine")
    _put(txn, "data", b"new")
    with pytest.raises(FileExistsError):
        txn.publish()
    assert leaf.read_text() == "occupant"
    assert [p.name for p in (root_dir / "out").iterdir()] == ["leaf"]
    swap = _out(rooted).begin("file", "replace", label="vault")
    _put(swap, "data", b"new")
    swap.publish()
    assert leaf.read_bytes() == b"new"
    assert [p.name for p in (root_dir / "out").iterdir()] == ["leaf"]


def test_file_exclusive_publishes_a_new_leaf_with_private_mode(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    txn = _out(rooted).begin("file", "exclusive", label="quarantine")
    _put(txn, "data", b"q")
    txn.publish()
    leaf = root_dir / "out" / "leaf"
    assert leaf.read_bytes() == b"q" and (leaf.stat().st_mode & 0o777) == 0o600


def test_ensure_dir_and_emptiness(rooted: RootedFileAccess, root_dir: Path) -> None:
    out = _out(rooted, "x/y/leaf")
    assert out.is_empty_or_absent()
    out.ensure_dir()
    out.ensure_dir()
    assert (root_dir / "x" / "y" / "leaf").is_dir() and out.is_empty_or_absent()
    (root_dir / "x" / "y" / "leaf" / "f").write_text("1")
    assert not out.is_empty_or_absent()


def test_no_stage_operation_uses_rename_exchange() -> None:
    import decoy_engine.fileaccess._output as mod

    assert "RENAME_EXCHANGE" not in Path(mod.__file__).read_text().replace(
        "No path uses `RENAME_EXCHANGE`", ""
    ).replace("`RENAME_EXCHANGE`", "")


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_every_name_taking_operation_validates_before_any_syscall(
    rooted: RootedFileAccess, root_dir: Path, tmp_path: Path, bad: str
) -> None:
    sentinel_dir = tmp_path / "beside"
    sentinel_dir.mkdir()
    (sentinel_dir / "s").write_text("S")
    before = _inodes(tmp_path)
    txn = _out(rooted).begin("dir", "exclusive", label="table_sink")
    _put(txn, "good")
    ops = [
        lambda: txn.create_file(bad),
        lambda: txn.open_file(bad),
        lambda: txn.create_dir(bad),
        lambda: txn.remove(bad),
        lambda: txn.rename(bad, "x"),
        lambda: txn.rename("good", bad),
    ]
    stage_listing = sorted(p.name for p in os.scandir(_stage_path(root_dir)))
    for op in ops:
        with pytest.raises(FileAccessDenied):
            op()
    nested = txn.create_dir("nested")
    for name_op in (nested.create_file, nested.open_file, nested.create_dir, nested.remove):
        with pytest.raises(FileAccessDenied):
            name_op(bad)
    assert sorted(p.name for p in os.scandir(_stage_path(root_dir))) == sorted(
        [*stage_listing, "nested"]
    )
    txn.abort()
    after = {k: v for k, v in _inodes(tmp_path).items() if "_decoy_stage_" not in k}
    assert {k: v for k, v in before.items() if k in after} == {
        k: v for k, v in after.items() if k in before
    }


def _stage_path(root_dir: Path) -> Path:
    (stage,) = [p for p in (root_dir / "out").iterdir() if p.name.startswith("_decoy_stage_")]
    return stage


def test_publish_validates_the_final_leaf(rooted: RootedFileAccess) -> None:
    for bad in ("..", ".", "a\0b", "n" * 256):
        with pytest.raises(FileAccessDenied):
            rooted.output_dir(FileRequest(FileRole.TARGET_OUTPUT, "write", f"out/{bad}", "k"))


def test_a_planted_symlink_in_the_stage_is_not_followed(
    rooted: RootedFileAccess, root_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("OUT")
    txn = _out(rooted).begin("dir", "exclusive", label="table_sink")
    os.symlink(outside, _stage_path(root_dir) / "planted")
    with pytest.raises(OSError):
        txn.create_file("planted")
    with pytest.raises(OSError):
        txn.open_file("planted")
    assert outside.read_text() == "OUT"
    txn.abort()


def test_stage_misuse_is_refused_without_touching_the_filesystem(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    out = _out(rooted)
    published = out.begin("dir", "exclusive", label="table_sink")
    _put(published, "a")
    published.publish()
    for op in (
        lambda: published.create_file("b"),
        lambda: published.publish(),
        lambda: published.create_dir("d"),
    ):
        with pytest.raises(FileAccessUnavailable):
            op()
    aborted = out.begin("dir", "exclusive", label="table_sink")
    aborted.abort()
    with pytest.raises(FileAccessUnavailable):
        aborted.create_file("x")
    with pytest.raises(FileAccessUnavailable):
        aborted.publish()
    assert sorted(p.name for p in (root_dir / "out").iterdir()) == ["leaf"]
    with pytest.raises(FileAccessUnavailable):
        RootedOutputTxn(
            object(), os.open(root_dir, os.O_RDONLY), "leaf", "dir", "exclusive", "table_sink", "k"
        )  # type: ignore[arg-type]


def test_a_transaction_travels_across_processes_only_as_its_descriptor(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    txn = _out(rooted).begin("dir", "exclusive", label="table_sink")
    import pickle

    with pytest.raises(Exception):
        pickle.dumps(txn)
    left, right = socket.socketpair()
    try:
        stage_fd = os.open(_stage_path(root_dir), os.O_RDONLY | os.O_DIRECTORY)
        socket.send_fds(left, [b"x"], [stage_fd])
        _, fds, _, _ = socket.recv_fds(right, 1, 1)
        os.close(stage_fd)
        os.close(fds[0])
    finally:
        left.close()
        right.close()
    txn.abort()


def test_local_stage_path_is_refused_under_a_strict_policy(rooted: RootedFileAccess) -> None:
    txn = _out(rooted).begin("dir", "exclusive", label="table_sink")
    with pytest.raises(FileAccessUnavailable):
        txn.local_stage_path()
    txn.abort()

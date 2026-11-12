"""The protected directory allocator (LIB L0.1): modes, reopen checks, policy stance, codes.

POSIX lanes only; the Windows DACL rows belong to L0.1w.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from decoy_engine import fileaccess, reasons
from decoy_engine.fileaccess import (
    FileRole,
    ProtectedDirAclFailed,
    ProtectedDirUnsafe,
    Root,
    RootedFileAccess,
    open_protected_dir,
    protected_scratch_dir,
)
from tests.fileaccess.support import ALL_ACCESS

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows lane is L0.1w")


class _Strict(RootedFileAccess):
    """A strict policy that also builds where `openat2` is missing (macOS)."""

    def __init__(self, root: Path) -> None:
        self.root_fd = os.open(root, os.O_RDONLY)
        super().__init__(
            [Root("tmp", self.root_fd, frozenset(FileRole), ALL_ACCESS)],
            allow_portable_fallback=True,
        )

    def close(self) -> None:
        os.close(self.root_fd)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def umask():  # type: ignore[no-untyped-def]
    old = os.umask(0)
    yield os.umask
    os.umask(old)


@pytest.mark.serial  # os.umask is process-wide
@pytest.mark.parametrize("mask", [0o000, 0o077, 0o277])
def test_p0_posix_mode_is_0700_whatever_the_umask(tmp_path: Path, umask, mask: int) -> None:  # type: ignore[no-untyped-def]
    umask(mask)
    with protected_scratch_dir(tmp_path, "t-") as root:
        assert _mode(root) == 0o700
    made = open_protected_dir(tmp_path / "long" / "lived", create=True, parents=True)
    assert _mode(made) == 0o700 and _mode(made.parent) == 0o700


def test_p0_removed_on_normal_exit_and_on_error(tmp_path: Path) -> None:
    with protected_scratch_dir(tmp_path, "t-") as root:
        (root / "child").mkdir()
        (root / "child" / "f").write_text("x")
    assert not root.exists()
    with pytest.raises(RuntimeError), protected_scratch_dir(tmp_path, "t-") as again:
        raise RuntimeError("boom")
    assert not again.exists()


def test_p0_reopen_changes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "lib"
    open_protected_dir(target, create=True)
    (target / "keep").write_text("rows")
    before = (_mode(target), sorted(os.listdir(target)))
    for _ in range(3):
        assert open_protected_dir(target, create=True) == target
    assert (_mode(target), sorted(os.listdir(target))) == before


def test_p0_permissive_empty_is_tightened(tmp_path: Path) -> None:
    target = tmp_path / "open"
    target.mkdir()
    os.chmod(target, 0o755)  # noqa: S103 - the test needs an open mode
    open_protected_dir(target, create=True)
    assert _mode(target) == 0o700


def test_p0_permissive_full_is_refused_and_left_as_it_was(tmp_path: Path) -> None:
    target = tmp_path / "open"
    target.mkdir()
    (target / "f").write_text("x")
    os.chmod(target, 0o755)  # noqa: S103 - the test needs an open mode
    with pytest.raises(ProtectedDirUnsafe):
        open_protected_dir(target, create=True)
    assert _mode(target) == 0o755


def test_p0_symlink_is_refused_and_target_untouched(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    os.chmod(real, 0o755)  # noqa: S103 - the test needs an open mode
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ProtectedDirUnsafe):
        open_protected_dir(link, create=True)
    assert _mode(real) == 0o755 and os.listdir(real) == []


def test_p0_not_a_directory_is_refused(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_text("x")
    with pytest.raises(ProtectedDirUnsafe):
        open_protected_dir(file, create=True)


def test_p0_foreign_owner_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "mine"
    open_protected_dir(target, create=True)
    monkeypatch.setattr(os, "geteuid", lambda: os.lstat(target).st_uid + 1)
    with pytest.raises(ProtectedDirUnsafe):
        open_protected_dir(target, create=True)


def test_p0_missing_without_create_is_a_plain_missing_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        open_protected_dir(tmp_path / "nope", create=False)
    assert not (tmp_path / "nope").exists()


def test_p0_fail_closed_leaves_no_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: object, **k: object) -> None:
        raise PermissionError(1, "injected /secret/path")

    monkeypatch.setattr(os, "chmod", broken)
    with pytest.raises(ProtectedDirAclFailed) as info:
        open_protected_dir(tmp_path / "x", create=True)
    assert not (tmp_path / "x").exists()
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert "secret" not in str(info.value) and str(tmp_path) not in str(info.value)
    with pytest.raises(ProtectedDirAclFailed):
        with protected_scratch_dir(tmp_path, "f-"):
            pass
    assert os.listdir(tmp_path) == []


def test_p0_unsafe_error_has_no_context_and_no_path(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_text("x")
    with pytest.raises(ProtectedDirUnsafe) as info:
        open_protected_dir(file, create=True)
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert str(tmp_path) not in str(info.value)


def test_p0_unwritable_parent_is_a_plain_oserror_without_the_path(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        with pytest.raises(OSError) as info:
            open_protected_dir(locked / "child", create=True)
    finally:
        os.chmod(locked, 0o700)
    assert not isinstance(info.value, ProtectedDirAclFailed)
    assert str(locked) not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_p0_strict_refused_caller_path_but_live_root_children_work(tmp_path: Path) -> None:
    jail = tmp_path / "jail"
    jail.mkdir()
    policy = _Strict(jail)
    try:
        with fileaccess.scope(policy):
            with pytest.raises(fileaccess.FileAccessUnavailable):
                open_protected_dir(tmp_path / "caller", create=True)
            with pytest.raises(fileaccess.FileAccessUnavailable):
                with protected_scratch_dir(tmp_path / "callerparent", "x-"):
                    pass
            assert not (tmp_path / "caller").exists()
            assert not (tmp_path / "callerparent").exists()
            with protected_scratch_dir(None, "s-") as root:
                child = open_protected_dir(root / "a" / "b", create=True, parents=True)
                assert _mode(child) == 0o700
                with protected_scratch_dir(root, "n-") as nested:
                    assert nested.parent == root
                escape = root / ".." / "escaped"
                with pytest.raises(fileaccess.FileAccessUnavailable):
                    open_protected_dir(escape, create=True)
                sibling = Path(str(root) + "x")
                with pytest.raises(fileaccess.FileAccessUnavailable):
                    open_protected_dir(sibling, create=True)
                assert not sibling.exists() and not (root.parent / "escaped").exists()
            with pytest.raises(fileaccess.FileAccessUnavailable):
                open_protected_dir(root / "again", create=True)  # root is no longer live
            assert not root.exists()
    finally:
        policy.close()


def test_p0_codes_are_registered_with_fixed_messages() -> None:
    for exc, code in (
        (ProtectedDirUnsafe, "scratch_unsafe"),
        (ProtectedDirAclFailed, "scratch_acl_failed"),
    ):
        assert reasons.is_registered(code, ["runtime"])
        assert exc.code == code and reasons.get("runtime", code).since == "1.7.0"
        err = exc()
        assert err.__cause__ is None and err.__context__ is None
        assert "/" not in str(err)
    assert issubclass(ProtectedDirUnsafe, ValueError)
    assert issubclass(ProtectedDirAclFailed, OSError)
    # Later additions raise the registry past the release these two codes joined.
    assert tuple(map(int, reasons.REGISTRY_VERSION.split("."))) >= (1, 7, 0)


# -- Dennis round 1: relative paths, concurrent creation, symlinks inside a live root ------------


def test_relative_paths_work_at_every_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    one = open_protected_dir("rel2", create=True, parents=True)
    deep = open_protected_dir("rel/a/b", create=True, parents=True)
    assert _mode(tmp_path / "rel2") == 0o700 and _mode(tmp_path / "rel" / "a") == 0o700
    assert one.is_absolute() and deep == tmp_path / "rel" / "a" / "b"
    with protected_scratch_dir("relparent/x", "r-") as root:
        assert root.parent == tmp_path / "relparent" / "x" and _mode(root) == 0o700
    assert not root.exists()


@pytest.mark.serial  # many threads racing on the filesystem
def test_concurrent_openers_of_a_missing_deep_path_all_succeed(tmp_path: Path) -> None:
    import threading

    errors: list[BaseException] = []

    def work(target: Path, barrier: threading.Barrier) -> None:
        try:
            barrier.wait()
            open_protected_dir(target, create=True, parents=True)
        except BaseException as exc:
            errors.append(exc)

    for trial in range(150):
        target = tmp_path / f"t{trial}" / "a" / "b"
        barrier = threading.Barrier(4)
        threads = [threading.Thread(target=work, args=(target, barrier)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []


@pytest.mark.serial
def test_concurrent_scratch_dirs_under_a_missing_parent_all_succeed(tmp_path: Path) -> None:
    import threading

    errors: list[BaseException] = []

    def work(parent: Path, barrier: threading.Barrier) -> None:
        try:
            barrier.wait()
            with protected_scratch_dir(parent, "c-"):
                pass
        except BaseException as exc:
            errors.append(exc)

    for trial in range(150):
        parent = tmp_path / f"p{trial}" / "x"
        barrier = threading.Barrier(4)
        threads = [threading.Thread(target=work, args=(parent, barrier)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []


def test_a_symlink_inside_a_live_root_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with protected_scratch_dir(tmp_path, "l-") as root:
        (root / "link").symlink_to(outside)
        with pytest.raises(ProtectedDirUnsafe):
            open_protected_dir(root / "link" / "new", create=True)
        with pytest.raises(ProtectedDirUnsafe):
            with protected_scratch_dir(root / "link", "n-"):
                pass
    assert os.listdir(outside) == []


def test_a_dotdot_component_inside_a_live_root_is_refused(tmp_path: Path) -> None:
    with protected_scratch_dir(tmp_path, "d-") as root:
        (root / "real").mkdir()
        with pytest.raises(ProtectedDirUnsafe):
            open_protected_dir(root / "real" / ".." / "phys", create=True)
        assert not (root / "phys").exists()

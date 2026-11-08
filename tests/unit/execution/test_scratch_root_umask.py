"""A caller-named scratch directory that does not exist yet works under any umask.

Every level the engine creates on the way to (and including) the run's root is 0700, so a umask
of 0777 cannot leave a level the engine itself cannot search before it creates the next one.
Root ignores directory mode bits, so these tests refuse to run as root rather than pass vacuously.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from decoy_engine.execution.out_of_core._scratch import engine_scratch_root, scratch_root

UMASKS = (0o000, 0o077, 0o777)


@pytest.fixture(params=UMASKS, ids=lambda m: f"umask{m:04o}")
def mask(request: pytest.FixtureRequest) -> int:
    if os.geteuid() == 0:
        pytest.fail(
            "run as a non-root user: root ignores directory modes, so this would be vacuous"
        )
    return request.param


@contextmanager
def _umask(value: int) -> Iterator[None]:
    old = os.umask(value)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _open_up(top: Path) -> None:
    # Lets pytest's tmp_path cleanup remove whatever a failing run left at mode 0000.
    os.chmod(top, 0o700)
    for child in top.iterdir():
        if child.is_dir() and not child.is_symlink():
            _open_up(child)


def test_engine_scratch_root_creates_missing_parent_under_any_umask(
    tmp_path: Path, mask: int
) -> None:
    parent = tmp_path / "a" / "b"
    assert not (tmp_path / "a").exists()  # non-vacuity: the engine creates both levels
    try:
        with _umask(mask):
            with engine_scratch_root(parent) as root:
                root_mode = _mode(root)
                created = {p: _mode(tmp_path / p) for p in ("a", "a/b")}
                assert root.parent == parent
            gone = not root.exists()
    finally:
        _open_up(tmp_path)
    assert root_mode == 0o700, (oct(mask), oct(root_mode))
    assert {p: oct(m) for p, m in created.items() if m != 0o700} == {}, oct(mask)
    assert gone, root


def test_scratch_root_creates_missing_levels_under_any_umask(tmp_path: Path, mask: int) -> None:
    temp_dir = tmp_path / "x" / "y" / "z"
    assert not (tmp_path / "x").exists()  # non-vacuity: the engine creates all three levels
    try:
        with _umask(mask):
            root = scratch_root(temp_dir)
        created = {p: _mode(tmp_path / p) for p in ("x", "x/y", "x/y/z")}
    finally:
        _open_up(tmp_path)
    assert root == temp_dir
    assert {p: oct(m) for p, m in created.items() if m != 0o700} == {}, oct(mask)

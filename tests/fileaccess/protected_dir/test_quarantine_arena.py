"""The quarantine arena goes through the allocator (LIB L0.1): unsafe arenas are reported, not raised."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.declared_casting import LocalProtectedQuarantineSink
from decoy_engine.keyprovider import SecretKeyProvider

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows lane is L0.1w")


def _detail(root: Path) -> tuple[bool, str]:
    cap: Any = LocalProtectedQuarantineSink(
        root, key_provider=SecretKeyProvider(secret=b"q" * 32)
    ).capability
    return cap.qualified, cap.detail


def test_a_symlinked_arena_is_unsafe(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "q").symlink_to(real)
    assert _detail(tmp_path / "q") == (False, "arena_unsafe")


def test_a_permissive_arena_holding_a_file_is_unsafe_but_an_empty_one_is_tightened(
    tmp_path: Path,
) -> None:
    full = tmp_path / "full"
    full.mkdir()
    (full / "f").write_text("x")
    os.chmod(full, 0o755)  # noqa: S103 - the test needs an open mode
    assert _detail(full) == (False, "arena_unsafe")
    empty = tmp_path / "empty"
    empty.mkdir()
    os.chmod(empty, 0o755)  # noqa: S103 - the test needs an open mode
    assert _detail(empty) == (True, "")
    assert oct(os.lstat(empty).st_mode & 0o777) == "0o700"


def test_a_foreign_owner_arena_is_unsafe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arena = tmp_path / "q"
    arena.mkdir(mode=0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.lstat(arena).st_uid + 1)
    assert _detail(arena) == (False, "arena_unsafe")


def test_an_unwritable_parent_is_unavailable(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        assert _detail(locked / "q") == (False, "arena_unavailable")
    finally:
        os.chmod(locked, 0o700)


def test_a_failed_protection_is_unavailable_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: object, **k: object) -> None:
        raise PermissionError(1, "injected")

    monkeypatch.setattr(os, "chmod", broken)
    assert _detail(tmp_path / "q") == (False, "arena_unavailable")
    assert not (tmp_path / "q").exists()

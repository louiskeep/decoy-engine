"""The allocator's parent rule and token loop, extracted for the scratch arena (characterization).

``protected_scratch_dir`` must behave exactly as before the extraction; the helpers are what a
second caller (the arena) will use, so their contracts are pinned here directly.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from decoy_engine import fileaccess
from decoy_engine.fileaccess import ProtectedDirUnsafe, protected_scratch_dir
from decoy_engine.fileaccess import _protected_dir as pd
from tests.fileaccess.protected_dir.test_protected_dir import _Strict

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows lane is L0.1w")


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_extraction_keeps_protected_scratch_dir(tmp_path: Path) -> None:
    with protected_scratch_dir(tmp_path / "a" / "b", "p-") as root:
        assert root.parent == tmp_path / "a" / "b"
        assert re.fullmatch(r"p-[0-9a-f]{16}", root.name)  # the 8-byte token
        assert _mode(root) == 0o700 and _mode(root.parent) == 0o700  # missing levels made
        assert pd._live_root(pd._key(root)) == pd._key(root)
    assert pd._live_root(pd._key(root)) is None
    assert not root.exists()
    with pytest.raises(ProtectedDirUnsafe):
        with protected_scratch_dir(tmp_path, f"x{os.sep}y"):
            pass


def test_parent_base_default_and_policy_stance(tmp_path: Path) -> None:
    assert pd._parent_base(None) == tempfile.gettempdir()
    jail = tmp_path / "jail"
    jail.mkdir()
    policy = _Strict(jail)
    try:
        with fileaccess.scope(policy):
            with pytest.raises(fileaccess.FileAccessUnavailable):
                pd._parent_base(tmp_path / "caller")  # a caller path needs the ambient policy
            assert not (tmp_path / "caller").exists()
            with protected_scratch_dir(None, "s-") as live:
                assert pd._parent_base(live / "sub") == str(live / "sub")  # live root: any policy
                assert _mode(live / "sub") == 0o700
    finally:
        policy.close()


def test_allocate_makes_a_fresh_unregistered_protected_directory(tmp_path: Path) -> None:
    first = pd._allocate(str(tmp_path), "decoy-arena-pending-")
    second = pd._allocate(str(tmp_path), "decoy-arena-pending-")
    assert first != second
    for made in (first, second):
        assert re.fullmatch(r"decoy-arena-pending-[0-9a-f]{16}", os.path.basename(made))
        assert _mode(Path(made)) == 0o700
        assert pd._live_root(pd._key(made)) is None  # the caller decides whether to register


def test_register_and_unregister_make_a_live_root(tmp_path: Path) -> None:
    root = pd._allocate(str(tmp_path), "r-")
    child = os.path.join(root, "child")
    pd._register(root)
    try:
        assert pd._live_root(pd._key(child)) == pd._key(root)
    finally:
        pd._unregister(root)
    assert pd._live_root(pd._key(child)) is None
    pd._unregister(root)  # idempotent


def test_owned_private_needs_the_effective_owner_and_no_group_or_other_bits(
    tmp_path: Path,
) -> None:
    target = tmp_path / "t"
    target.mkdir()
    os.chmod(target, 0o700)
    assert pd._owned_private(os.lstat(target))
    os.chmod(target, 0o750)  # noqa: S103 - the test needs an open mode
    assert not pd._owned_private(os.lstat(target))
    os.chmod(target, 0o701)
    assert not pd._owned_private(os.lstat(target))
    os.chmod(target, 0o700)
    foreign = os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 1, os.geteuid() + 1, 0, 0, 0, 0, 0))
    assert not pd._owned_private(foreign)

"""F22 5.1: `OutputDir.lock`, `OutputDir.read_member` and the strict confinement of both.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 rows
`test_lock_contention_and_unsupported` and `test_strict_policy_confined` (slice a1b). The lock is
`flock(LOCK_EX)` on `.<leaf>.decoy-lock` in the leaf's parent, polled to a deadline, never
unlinked; there is no lock-free fallback.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sys
import time
from pathlib import Path

import pytest

from tests.fileaccess.publication._chain_support import OSMARK, mode, output_dir

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock")

POLICIES = ["strict", "ambient"]


@pytest.mark.parametrize("policy", POLICIES)
def test_lock_contention_and_unsupported(
    tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine import fileaccess

    base = tmp_path / "root"
    (base / "x").mkdir(parents=True)
    out, root_fd = output_dir(policy, base, "x/out")
    lock_file = base / "x" / ".out.decoy-lock"
    try:
        with out.lock(5.0):
            probe = os.open(lock_file, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):  # non-vacuity: the lock is really held
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            start = time.monotonic()
            with pytest.raises(fileaccess.LockTimeout):  # type: ignore[attr-defined]
                with out.lock(0.2):
                    pass
            assert time.monotonic() - start >= 0.2
        assert mode(lock_file) == 0o600
        with out.lock(0.1):  # released on exit
            pass

        def unsupported(fd: int, op: int) -> None:
            raise OSError(errno.ENOLCK, f"no locks {OSMARK}")

        monkeypatch.setattr(fcntl, "flock", unsupported)
        with pytest.raises(fileaccess.LockUnavailable) as caught:  # type: ignore[attr-defined]
            with out.lock(1.0):
                pass
        assert caught.value.errno == errno.ENOLCK
        text = str(caught.value)
        assert OSMARK not in text and str(base) not in text and lock_file.name not in text
        assert lock_file.exists()  # never unlinked
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def test_strict_policy_confined(tmp_path: Path) -> None:
    from decoy_engine import fileaccess

    base, outside = tmp_path / "root", tmp_path / "outside"
    (base / "real" / "out").mkdir(parents=True)
    (outside / "out").mkdir(parents=True)
    (outside / "out" / "m.json").write_bytes(b"OUTSIDE")
    (base / "real" / "out" / "m.json").write_bytes(b"INSIDE")
    os.symlink(outside, base / "lnk")
    linked, root_fd = output_dir("strict", base, "lnk/out")
    try:
        with pytest.raises(fileaccess.LockUnavailable):  # type: ignore[attr-defined]
            with linked.lock(0.1):
                pass
        with pytest.raises(OSError):
            linked.read_member("m.json", limit=64)
        with pytest.raises(OSError):
            linked.sync_chain()
        with pytest.raises(fileaccess.FileAccessDenied):
            linked.begin("dir", "exclusive", label="publication")
        assert sorted(os.listdir(outside)) == ["out"]  # nothing made through the link

        inside, inside_fd = output_dir("strict", base, "real/out")
        try:
            with inside.lock(0.1):
                assert (base / "real" / ".out.decoy-lock").is_file()
            assert inside.read_member("m.json", limit=64) == b"INSIDE"
        finally:
            os.close(inside_fd)
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("policy", POLICIES)
def test_read_member_bounded_and_absent(tmp_path: Path, policy: str) -> None:
    from decoy_engine import fileaccess

    base = tmp_path / "root"
    (base / "out").mkdir(parents=True)
    (base / "out" / "m.json").write_bytes(b"0123456789")
    os.mkfifo(base / "out" / "fifo")
    out, root_fd = output_dir(policy, base, "out")
    missing, missing_fd = output_dir(policy, base, "none/out")
    try:
        assert out.read_member("m.json", limit=10) == b"0123456789"
        assert out.read_member("absent.json", limit=10) is None
        assert missing.read_member("m.json", limit=10) is None
        with pytest.raises(fileaccess.LeafTooLarge):  # type: ignore[attr-defined]
            out.read_member("m.json", limit=9)
        with pytest.raises(OSError):  # a FIFO is not read (and the open does not block)
            out.read_member("fifo", limit=10)
        with pytest.raises(fileaccess.FileAccessDenied):
            out.read_member("../m.json", limit=10)
        with pytest.raises(fileaccess.FileAccessUnavailable):  # a publication is a directory
            out.begin("file", "exclusive", label="publication")
    finally:
        for fd in (root_fd, missing_fd):
            if fd >= 0:
                os.close(fd)

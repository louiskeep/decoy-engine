"""F22 5.1 fault paths: descriptors on a failed `begin`, value-free errors, and the leaf limit.

A strict `begin(label="publication")` that cannot make its stage closes every descriptor it
opened. `sync_chain()` keeps only an errno from its mount-boundary probe. The four errno-only
errors carry no cause and no context. A publication destination leaf is at most
`MAX_PUBLICATION_LEAF_BYTES` (243) bytes, so `.<leaf>.decoy-lock` fits one 255-byte component;
a longer leaf is denied by `lock()` and `begin(..., label="publication")` before any I/O.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.fileaccess._openat2 import openat2_usable
from tests.fileaccess.publication._chain_support import (
    OSMARK,
    UNIT_BYTES,
    UNIT_FILE,
    Recorder,
    anchor_at,
    key,
    output_dir,
    stage_unit,
)
from tests.fileaccess.publication._dirmode_support import POLICIES

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durable publish")
strict_only = pytest.mark.skipif(not openat2_usable(), reason="strict policy needs Linux openat2")


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _assert_bare(exc: BaseException) -> None:
    assert exc.__cause__ is None and exc.__context__ is None


@strict_only
@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="counts fds through /proc")
@pytest.mark.parametrize("cause", ["mode_0500", "mkdir_enospc", "dup_emfile"])
def test_failed_begin_closes_its_descriptors(
    tmp_path: Path, cause: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.fileaccess import _output

    base = tmp_path / "root"
    parent = base / "a" / "b"
    parent.mkdir(parents=True)
    out, root_fd = output_dir("strict", base, "a/b/out")
    fired: list[str] = []
    if cause == "mode_0500":
        if os.geteuid() == 0:
            pytest.skip("root ignores 0500; run as a non-root uid")
        os.chmod(parent, 0o500)
        fired.append("0500")
    elif cause == "mkdir_enospc":
        real_make = _output.make_stage_dir_at

        def full(parent_fd: int, name: str) -> int:
            fired.append(name)
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

        monkeypatch.setattr(_output, "make_stage_dir_at", full)
        assert real_make is not full
    else:
        real_dup = _output._dup

        def second_fails(fd: int) -> int:
            fired.append("dup")
            if len(fired) % 2 == 0:  # the second of the two chain dups in each attempt
                raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
            return real_dup(fd)

        monkeypatch.setattr(_output, "_dup", second_fails)
    try:
        before = _open_fds()
        for _ in range(3):
            with pytest.raises(OSError):
                out.begin("dir", "exclusive", label="publication")
        assert fired  # non-vacuity: the named failure happened
        assert _open_fds() == before
        assert not list(parent.iterdir())
    finally:
        os.chmod(parent, 0o700)
        os.close(root_fd)


def test_sync_chain_probe_error_is_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "root"
    (base / "x" / "out").mkdir(parents=True)
    out, _ = output_dir("ambient", base, "x/out")
    real_stat = os.stat
    hit: list[str] = []

    def probe(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if kwargs.get("follow_symlinks") is False and str(base) in os.fsdecode(path):
            hit.append("stat")
            raise FileNotFoundError(2, "x", "/SECRET")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", probe)
    with pytest.raises(OSError) as caught:
        out.sync_chain()
    assert hit  # non-vacuity: the probe's stat raised
    exc = caught.value
    assert "/SECRET" not in str(exc) and exc.filename is None
    assert exc.errno == errno.ENOENT
    _assert_bare(exc)


@pytest.mark.parametrize("policy", POLICIES)
def test_errno_only_errors_carry_no_chain(
    tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine import fileaccess

    base = tmp_path / "root"
    (base / "x").mkdir(parents=True)
    (base / "x" / "big").mkdir()
    (base / "x" / "big" / "m.json").write_bytes(b"0123456789")
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    out, root_fd = output_dir(policy, base, "x/out")
    big, big_fd = output_dir(policy, base, "x/big")
    gone, gone_fd = output_dir(policy, base, "none/out")
    try:
        with out.lock(5.0):
            with pytest.raises(fileaccess.LockTimeout) as timed:  # type: ignore[attr-defined]
                with out.lock(0.1):
                    pass
        _assert_bare(timed.value)
        with pytest.raises(fileaccess.LockUnavailable) as no_parent:  # type: ignore[attr-defined]
            with gone.lock(0.1):
                pass
        assert no_parent.value.errno == errno.ENOENT
        _assert_bare(no_parent.value)
        with pytest.raises(fileaccess.LeafTooLarge) as large:  # type: ignore[attr-defined]
            big.read_member("m.json", limit=9)
        _assert_bare(large.value)
        txn = out.begin("dir", "exclusive", label="publication")
        stage_unit(txn)
        parent_key = key(base / "x")
        Recorder(monkeypatch, lambda event: event == ("fsync", parent_key))
        with pytest.raises(fileaccess.CommitUncertain) as uncertain:  # type: ignore[attr-defined]
            txn.publish()
        _assert_bare(uncertain.value)
        assert (base / "x" / "out" / UNIT_FILE).read_bytes() == UNIT_BYTES  # bound, kept

        def unsupported(fd: int, op: int) -> None:
            raise OSError(errno.ENOLCK, f"no locks {OSMARK}")

        monkeypatch.setattr(fcntl, "flock", unsupported)
        with pytest.raises(fileaccess.LockUnavailable) as no_flock:  # type: ignore[attr-defined]
            with out.lock(0.1):
                pass
        assert no_flock.value.errno == errno.ENOLCK
        _assert_bare(no_flock.value)
    finally:
        for fd in (root_fd, big_fd, gone_fd):
            if fd >= 0:
                os.close(fd)


def _spy_io(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from decoy_engine.fileaccess import _openat2

    calls: list[str] = []

    def spy(name: str, real: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return real(*args, **kwargs)

        return call

    for name in ("open", "mkdir", "stat", "lstat", "dup"):
        monkeypatch.setattr(os, name, spy(name, getattr(os, name)))
    monkeypatch.setattr(_openat2, "_confined_open", spy("openat2", _openat2._confined_open))
    return calls


LEAVES = {
    "243": "a" * 243,
    "244": "a" * 244,
    "244_multibyte": "é" * 122,  # 122 characters, 244 bytes: the limit counts bytes
}


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("size", sorted(LEAVES))
def test_publication_leaf_limit(
    tmp_path: Path, policy: str, size: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine import fileaccess

    leaf = LEAVES[size]
    base = tmp_path / "root"
    (base / "new").mkdir(parents=True)
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    out, root_fd = output_dir(policy, base, f"new/{leaf}")
    deep, deep_fd = output_dir(policy, base, f"new/sub/{leaf}")
    try:
        if size == "243":
            with out.lock(5.0):
                txn = out.begin("dir", "exclusive", label="publication")
                stage_unit(txn)
                txn.publish()
            assert (base / "new" / leaf / UNIT_FILE).read_bytes() == UNIT_BYTES
            lock_name = f".{leaf}.decoy-lock"
            assert len(lock_name) == 255 and (base / "new" / lock_name).is_file()
            return
        calls = _spy_io(monkeypatch)
        with pytest.raises(fileaccess.FileAccessDenied) as locked:
            with out.lock(5.0):
                pass
        with pytest.raises(fileaccess.FileAccessDenied) as begun:
            deep.begin("dir", "exclusive", label="publication")
        assert calls == []  # refused before any I/O
        for exc in (locked.value, begun.value):
            assert leaf not in str(exc) and str(base) not in str(exc)
        monkeypatch.undo()
        assert os.listdir(base / "new") == []  # nothing made: no lock file, no parent, no stage
        txn = deep.begin("dir", "exclusive", label="table_sink")  # only publication is limited
        txn.abort()
    finally:
        for fd in (root_fd, deep_fd):
            if fd >= 0:
                os.close(fd)

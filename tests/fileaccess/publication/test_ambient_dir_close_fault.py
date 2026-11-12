"""A fault closing a directory descriptor never replaces the outcome of an ambient output call.

The ambient policy opens the directories on an output chain to make parents and stages under
them (F22 5.0 rule 1). Those descriptors are only read; a `close(2)` error on one carries no
data, so it must neither fail a write that otherwise succeeds nor mask the error that ended
one. The strict policy already closes through the best-effort `_close`; this holds the ambient
arm to the same contract.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

import pytest

from decoy_engine.fileaccess import FileRequest, FileRole
from decoy_engine.fileaccess._local import LocalFileAccess, LocalOutputDir

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory descriptors")

UNIT = "t.parquet"
DATA = b"F22-A1A-CLOSE"


def _faulty_directory_close(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make every close of a directory descriptor fail after it really closed, as a flaky
    filesystem would; return the list of faults raised (non-vacuity)."""
    real_close = os.close
    faults: list[int] = []

    def close(fd: int) -> None:
        try:
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        except OSError:
            is_dir = False
        real_close(fd)
        if is_dir:
            faults.append(fd)
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "close", close)
    return faults


def _output_dir(target: Path) -> LocalOutputDir:
    req = FileRequest(FileRole.TARGET_OUTPUT, "write", str(target), "targets.t.path")
    return LocalFileAccess().output_dir(req)


@pytest.mark.parametrize("op", ["publish", "ensure_dir", "create_dir"])
def test_directory_close_fault_does_not_fail_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    target = tmp_path / "p" / "a" / "out"
    out = _output_dir(target)
    faults = _faulty_directory_close(monkeypatch)
    if op == "ensure_dir":
        out.ensure_dir()
    else:
        txn = out.begin("dir", "exclusive", label="table_sink")
        if op == "create_dir":
            txn.create_dir("sub")
        fd = txn.create_file(UNIT)
        os.write(fd, DATA)
        os.close(fd)  # a regular file: the spy closes it without a fault
        txn.publish()
    monkeypatch.undo()
    assert faults, "non-vacuity: a directory close fault was injected"
    assert target.is_dir()
    if op != "ensure_dir":
        assert (target / UNIT).read_bytes() == DATA
        assert (target / "sub").is_dir() is (op == "create_dir")


def test_directory_close_fault_does_not_mask_the_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import decoy_engine.fileaccess._local as local

    def refuse(parent_fd: int, name: str) -> int:
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))

    out = _output_dir(tmp_path / "p" / "out")
    monkeypatch.setattr(local, "make_stage_dir_at", refuse)
    faults = _faulty_directory_close(monkeypatch)
    with pytest.raises(OSError) as raised:
        out.begin("dir", "exclusive", label="table_sink")
    monkeypatch.undo()
    assert faults, "non-vacuity: the parent descriptor close raised"
    assert raised.value.errno == errno.EACCES
    assert not list((tmp_path / "p").iterdir())

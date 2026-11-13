"""A private (0600) artifact keeps its exact mode whatever the process umask.

`open(2)` and `mkstemp` apply `mode & ~umask`, so a umask that masks owner bits would publish a
quarantine sidecar its owner cannot reopen. Both policies restore the mode with `fchmod` on the
descriptor before the first byte; the vault cases live in `tests/unit/test_vault_publish.py`.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileRequest, FileRole, RootedFileAccess
from decoy_engine.quarantine import publish_staged_jsonl, write_jsonl_staged
from decoy_engine.vault import VaultWriter

UMASKS = pytest.mark.parametrize(
    "umask", [0o000, 0o077, 0o700, 0o770, 0o777], ids=lambda m: f"umask{m:03o}"
)
RECORDS = [{"a": 1, "b": "x"}]


@UMASKS
def test_ambient_quarantine_sidecar_is_0600(tmp_path: Path, umask: int) -> None:
    final = tmp_path / "q.jsonl"
    previous = os.umask(umask)
    try:
        publish_staged_jsonl(write_jsonl_staged(str(final), RECORDS), str(final))
    finally:
        os.umask(previous)
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert [json.loads(x) for x in final.read_text().splitlines()] == RECORDS


@UMASKS
def test_strict_quarantine_txn_is_0600(
    rooted: RootedFileAccess, root_dir: Path, umask: int
) -> None:
    (root_dir / "out").mkdir()
    out = rooted.output_dir(
        FileRequest(FileRole.QUARANTINE_OUTPUT, "write", "out/q.jsonl", "quarantine.output_path")
    )
    previous = os.umask(umask)
    try:
        txn = out.begin("file", "exclusive", label="quarantine")
        fd = txn.create_file("data")
        os.write(fd, b"q")
        os.close(fd)
        txn.publish()
    finally:
        os.umask(previous)
    leaf = root_dir / "out" / "q.jsonl"
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o600
    assert leaf.read_bytes() == b"q"


def _fail_fchmod(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(fd: int, mode: int) -> None:
        raise OSError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(os, "fchmod", refuse)


def test_ambient_fchmod_failure_leaves_no_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    txn = (
        fileaccess.current()
        .output_dir(
            FileRequest(
                FileRole.QUARANTINE_OUTPUT, "write", str(tmp_path / "q.jsonl"), "quarantine"
            )
        )
        .begin("file", "exclusive", label="quarantine")
    )
    _fail_fchmod(monkeypatch)
    with pytest.raises(OSError):
        txn.create_file("sidecar")  # the txn removes its own stage; no caller abort
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(OSError):
        write_jsonl_staged(str(tmp_path / "q.jsonl"), RECORDS)
    with pytest.raises(PermissionError) as vault_info:  # stage creation keeps the ambient shape
        _vault().write(str(tmp_path / "v.dcyvault"))
    assert vault_info.value.errno == errno.EPERM
    assert vault_info.value.filename == str(tmp_path / "v.dcyvault")
    assert list(tmp_path.iterdir()) == []


def test_strict_fchmod_failure_leaves_no_stage(
    rooted: RootedFileAccess, root_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root_dir / "out").mkdir()
    out = rooted.output_dir(
        FileRequest(FileRole.QUARANTINE_OUTPUT, "write", "out/q.jsonl", "quarantine.output_path")
    )
    txn = out.begin("file", "exclusive", label="quarantine")
    _fail_fchmod(monkeypatch)
    with pytest.raises(OSError) as info:
        txn.create_file("data")
    assert info.value.errno == errno.EPERM and info.value.filename is None
    vault_out = rooted.output_dir(FileRequest(FileRole.VAULT, "write", "v.dcyvault", "vault"))
    with pytest.raises(PermissionError) as vault_info:  # stage creation keeps the strict shape
        _vault(rooted).write(vault_out)
    assert vault_info.value.errno == errno.EPERM and vault_info.value.filename is None
    assert list((root_dir / "out").iterdir()) == []
    assert sorted(p.name for p in root_dir.iterdir()) == ["out"]


def _vault(policy: RootedFileAccess | None = None) -> VaultWriter:
    writer = VaultWriter(b"12345678" * 4, file_access=policy)
    writer.add([("ns", "m", "s")])
    return writer

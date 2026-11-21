"""Helpers for the F22 b1 vault tests (plan 5.5, 7.2): a vault `OutputDir` under both policies,
a writer, and the probe that records what had been fsynced when a `VaultCommit` was issued.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import vault as vault_package
from decoy_engine.fileaccess import FileRequest, FileRole, Root, RootedFileAccess
from decoy_engine.fileaccess._local import LocalFileAccess
from decoy_engine.vault import VaultWriter
from decoy_engine.vault import _writer as vault_module
from tests.unit.vault._support import vault_writer

KEY = b"12345678" * 4
OTHER_KEY = b"abcdefgh" * 4
LEAF = "v.dcyvault"
ROWS = (("ns", "m1", "s1"), ("ns", "m2", "s2"))
OTHER_ROWS = (("ns", "m1", "s1"), ("ns", "m2", "DIFFERENT"))
CHILD_MARKER = "vault-child-killed"


def vault_out(policy: str, base: Path, rel: str) -> tuple[Any, int]:
    """The vault `OutputDir` for `base/rel` under the policy; the second value is the root fd."""
    if policy == "ambient":
        req = FileRequest(FileRole.VAULT, "write", str(base / rel), "vault")
        return LocalFileAccess().output_dir(req), -1
    root_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    roots = [Root("main", root_fd, frozenset({FileRole.VAULT}), frozenset({"write"}))]
    req = FileRequest(FileRole.VAULT, "write", rel, "vault")
    return RootedFileAccess(roots).output_dir(req), root_fd


def writer(rows: Any = ROWS, key: bytes = KEY) -> VaultWriter:
    made = vault_writer(key)
    made.add(list(rows))
    return made


def fingerprint(path: Path) -> tuple[str, int, int]:
    """The bytes' sha256, the size and the inode: what a refusal must leave unchanged."""
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data), os.stat(path).st_ino


class IssueProbe:
    """Records every `VaultCommit` the vault module issues, with the fsyncs seen by then."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, synced: Any) -> None:
        self.issued: list[Any] = []
        self.synced_at_issue: list[list[Any]] = []
        real = vault_module.issue_commit

        def spy(*args: Any, **kwargs: Any) -> Any:
            self.synced_at_issue.append(list(synced()))
            commit = real(*args, **kwargs)
            self.issued.append(commit)
            return commit

        # `VaultWriter.publish` calls the writer module's name; the adopt path calls it through
        # the package, so both bindings are patched.
        monkeypatch.setattr(vault_module, "issue_commit", spy)
        monkeypatch.setattr(vault_package, "issue_commit", spy)


def kill_child(spec_path: str) -> None:
    """Publish a vault, SIGKILLing this process after the `kill_at`-th fsync that follows the
    exclusive `link(2)` (the commit-point directory fsync is the first of them)."""
    spec = json.loads(Path(spec_path).read_text("utf-8"))
    base = Path(spec["base"])
    out, _ = vault_out(spec["policy"], base, spec["rel"])
    state = {"linked": False, "after": 0}
    real_link, real_fsync = os.link, os.fsync

    def link(*args: Any, **kwargs: Any) -> None:
        real_link(*args, **kwargs)
        state["linked"] = True

    def fsync(fd: int) -> None:
        real_fsync(fd)
        if state["linked"]:
            state["after"] += 1
            if state["after"] == spec["kill_at"]:
                (base.parent / CHILD_MARKER).write_text("x", encoding="utf-8")
                os.kill(os.getpid(), signal.SIGKILL)

    os.link, os.fsync = link, fsync  # type: ignore[assignment]
    writer().publish(out)

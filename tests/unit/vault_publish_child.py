"""Child process for the vault publication crash table (B-6 D1, SQLite `crashtest` pattern).

Usage: python vault_publish_child.py <ambient|strict> <K1..K7> <dir>

Wraps `os.open`, `os.write`, `os.fsync`, `os.link` and `os.unlink` on the `os` module BEFORE
the engine is imported (the file-access modules call them as `os.<name>` at call time), runs
one `VaultWriter.write`, and sends itself SIGKILL right after the wrapped call of the chosen
step returned. SIGKILL runs no `finally`, `__del__` or `weakref.finalize`, which an in-process
`raise` cannot model. K7 kills nothing: the write completes and the child exits 0. No
production seam is added. Facts the parent needs (the bytes the killed write returned) go to
`<dir>/_child_facts.json`, written before the kill.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys

ROUTE, POINT, DIRECTORY = sys.argv[1], sys.argv[2], sys.argv[3]
LEAF = "v.dcyvault"
STAGE_PREFIX = "_decoy_vault_stage_"
KEY = b"12345678" * 4
ENTRIES = [("ns", f"m{i}", f"s{i}") for i in range(50)]

_real = {name: getattr(os, name) for name in ("open", "write", "fsync", "link", "unlink")}
_state: dict[str, int] = {"stage_fd": -1, "linked": 0}


def _facts(**kw: object) -> None:
    with open(os.path.join(DIRECTORY, "_child_facts.json"), "w", encoding="utf-8") as fh:
        json.dump(kw, fh)


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _base(path: object) -> str:
    return os.path.basename(os.fsdecode(path)) if isinstance(path, (str, bytes)) else ""


def _open(path, flags, *args, **kw):  # type: ignore[no-untyped-def]
    fd = _real["open"](path, flags, *args, **kw)
    if _base(path).startswith(STAGE_PREFIX) and flags & os.O_CREAT:
        _state["stage_fd"] = fd
        if POINT == "K1":
            _die()
    return fd


def _write(fd, data):  # type: ignore[no-untyped-def]
    n = _real["write"](fd, data)
    if POINT == "K2" and fd == _state["stage_fd"]:
        _facts(written=n)
        _die()
    return n


def _fsync(fd):  # type: ignore[no-untyped-def]
    _real["fsync"](fd)
    if POINT == "K3" and fd == _state["stage_fd"]:
        _die()
    if POINT == "K5" and _state["linked"] and stat.S_ISDIR(os.fstat(fd).st_mode):
        _die()


def _link(src, dst, *args, **kw):  # type: ignore[no-untyped-def]
    _real["link"](src, dst, *args, **kw)
    if _base(dst) == LEAF:
        _state["linked"] = 1
        if POINT == "K4":
            _die()


def _unlink(path, *args, **kw):  # type: ignore[no-untyped-def]
    _real["unlink"](path, *args, **kw)
    if POINT == "K6" and _base(path).startswith(STAGE_PREFIX):
        _die()


os.open, os.write, os.fsync, os.link, os.unlink = _open, _write, _fsync, _link, _unlink  # type: ignore[assignment]

from decoy_engine.fileaccess import FileRequest, FileRole, Root, RootedFileAccess  # noqa: E402
from decoy_engine.vault import VaultWriter  # noqa: E402

if ROUTE == "ambient":
    writer = VaultWriter(KEY)
    writer.add(ENTRIES)
    writer.write(os.path.join(DIRECTORY, LEAF))
else:
    root = _real["open"](DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
    policy = RootedFileAccess(
        [Root("main", root, frozenset({FileRole.VAULT}), frozenset({"read", "write", "stat"}))]
    )
    writer = VaultWriter(KEY, file_access=policy)
    writer.add(ENTRIES)
    writer.write(policy.output_dir(FileRequest(FileRole.VAULT, "write", LEAF, "vault")))
sys.exit(0)

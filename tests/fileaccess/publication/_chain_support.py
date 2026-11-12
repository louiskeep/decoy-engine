"""In-process spies for the F22 durable-publish tests (plan 5.0, 5.1, 7.2; slice a1b).

`Recorder` logs, in order, every `os.fsync` (by the `(st_dev, st_ino)` of the descriptor), every
`os.mkdir` (by the key of what it made) and every `os.rename`/`os.replace` of a directory (by the
key of what moved), so a test can check order and run the crash model in `lost_entry`.
"""

from __future__ import annotations

import errno
import itertools
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.fileaccess.publication._dirmode_support import _output_dir, _resolve

UNIT_FILE = "t.parquet"
UNIT_BYTES = b"F22-A1B-UNIT"
OSMARK = "OSMARK"
STAGE_GLOB = "._decoy_publication_pending_*"
Key = tuple[int, int]
Event = tuple[str, Key]


def key(path: Path) -> Key:
    st = os.lstat(path)
    return (st.st_dev, st.st_ino)


def eio() -> OSError:
    return OSError(errno.EIO, f"injected {OSMARK}")


class Recorder:
    """Log fsync, mkdir and rename events; `fail(event)` returning True raises EIO instead."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, fail: Callable[[Event], bool] | None = None
    ):
        self.events: list[Event] = []
        self.fail = fail or (lambda _e: False)
        real_fsync, real_mkdir = os.fsync, os.mkdir

        def fsync(fd: int) -> None:
            st = os.fstat(fd)
            event = ("fsync", (st.st_dev, st.st_ino))
            if self.fail(event):
                raise eio()
            self.events.append(event)
            real_fsync(fd)

        def mkdir(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            real_mkdir(path, mode, dir_fd=dir_fd)
            st = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            self.events.append(("mkdir", (st.st_dev, st.st_ino)))

        def moving(real: Callable[..., None]) -> Callable[..., None]:
            def move(src: Any, dst: Any, *, src_dir_fd: Any = None, dst_dir_fd: Any = None) -> None:
                st = os.stat(src, dir_fd=src_dir_fd, follow_symlinks=False)
                event = ("rename", (st.st_dev, st.st_ino))
                if self.fail(event):
                    raise eio()
                real(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
                self.events.append(event)

            return move

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "mkdir", mkdir)
        monkeypatch.setattr(os, "rename", moving(os.rename))
        monkeypatch.setattr(os, "replace", moving(os.replace))

    def mark(self, path: Path) -> None:
        """Record that `path` was just linked into its parent (a file written into a stage)."""
        self.events.append(("staged", key(path)))

    def synced(self) -> list[Key]:
        return [k for kind, k in self.events if kind == "fsync"]

    def after_rename(self) -> list[Key]:
        kinds = [kind for kind, _ in self.events]
        cut = len(kinds) - kinds[::-1].index("rename")
        return [k for kind, k in self.events[cut:] if kind == "fsync"]


def lost_entry(events: list[Event], chain: list[Path]) -> Path | None:
    """The crash model: an entry survives a power cut only if its parent directory was fsynced
    after the event that linked it there (entries made before the log count as linked at -1).
    Return the first entry of `chain` (outermost first) that would be lost, or None."""
    for parent, child in itertools.pairwise(chain):
        child_key, parent_key = key(child), key(parent)
        made = max(
            (i for i, (kind, k) in enumerate(events) if k == child_key and kind != "fsync"),
            default=-1,
        )
        if ("fsync", parent_key) not in events[made + 1 :]:
            return child
    return None


def restat(st: os.stat_result, *, dev: int | None = None, uid: int | None = None) -> os.stat_result:
    fields = list(st)[:10]
    if dev is not None:
        fields[2] = dev
    if uid is not None:
        fields[4] = uid
    return os.stat_result(fields)


def anchor_at(monkeypatch: pytest.MonkeyPatch, anchor: Path) -> None:
    """Fix the ambient mount boundary at `anchor`: every directory above it reports another
    device through the walk's `st_dev` probe."""
    from decoy_engine.fileaccess import _local

    real = _local._probe_dir  # type: ignore[attr-defined]
    top = os.path.realpath(anchor)

    def probe(path: Any) -> os.stat_result:
        st = real(path)
        where = os.path.realpath(path)
        inside = where == top or where.startswith(top + os.sep)
        return st if inside else restat(st, dev=st.st_dev + 1)

    monkeypatch.setattr(_local, "_probe_dir", probe)


def deny_reads(monkeypatch: pytest.MonkeyPatch, denied: Path, seen: list[str]) -> None:
    """`EACCES` on any read open of `denied`, as for a same-uid 0300 directory under any uid
    (root ignores the mode). Confined opens go through `os.open` so they are covered too."""
    from decoy_engine.fileaccess import _openat2

    monkeypatch.setattr(_openat2, "FORCE_PORTABLE", True)
    real_open = os.open
    target = os.path.realpath(denied)

    def guarded(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        reading = (flags & os.O_ACCMODE) == os.O_RDONLY and not flags & os.O_PATH
        if reading and os.path.realpath(_resolve(path, dir_fd)) == target:
            seen.append("EACCES")
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", guarded)


def output_dir(policy: str, base: Path, rel: str) -> tuple[Any, int]:
    return _output_dir(policy, base, rel)


def stage_unit(txn: Any, rec: Recorder | None = None, leaf_parent: Path | None = None) -> None:
    """One unit file and one subdirectory holding one file, written into a publication stage."""
    fd = txn.create_file(UNIT_FILE)
    os.write(fd, UNIT_BYTES)
    os.close(fd)
    sub = txn.create_dir("sub")
    os.close(sub.create_file("g"))
    if rec is not None and leaf_parent is not None:
        (stage,) = [p for p in leaf_parent.glob(STAGE_GLOB) if (p / UNIT_FILE).exists()]
        rec.mark(stage / UNIT_FILE)


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)

"""F22 5.1: the `publication` label publishes a directory durably, on both POSIX policies.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 rows
`test_durable_dir_publish_fsync_order` and `test_durable_publish_faults` (slice a1b): every staged
file and directory is fsynced deepest first, then the rename, then the parent (the commit point)
and each directory above it up to and including the anchor, pre-existing ones included. The
strict anchor is the capability root; the ambient one is the mount boundary, fixed here at the
test root by patching the walk's `st_dev` probe.
"""

from __future__ import annotations

import errno
import importlib
import os
import sys
from pathlib import Path

import pytest

from tests.fileaccess.publication._chain_support import (
    OSMARK,
    STAGE_GLOB,
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


def _fileaccess() -> object:
    return importlib.import_module("decoy_engine.fileaccess")


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "x").mkdir(parents=True)  # `x` pre-exists; `y` is made by `begin()`
    return root


def _setup(base: Path, policy: str, monkeypatch: pytest.MonkeyPatch) -> tuple[object, int]:
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    return output_dir(policy, base, "x/y/out")


@pytest.mark.parametrize("policy", POLICIES)
def test_durable_dir_publish_fsync_order(
    base: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, root_fd = _setup(base, policy, monkeypatch)
    try:
        txn = out.begin("dir", "exclusive", label="publication")  # type: ignore[attr-defined]
        stage_unit(txn)
        rec = Recorder(monkeypatch)
        txn.publish()
        leaf = base / "x" / "y" / "out"
        tree = [leaf / "sub" / "g", leaf / "sub", leaf / UNIT_FILE, leaf]
        synced = rec.synced()
        assert ("rename", key(leaf)) in rec.events  # non-vacuity: the spy saw the bind
        cut = rec.events.index(("rename", key(leaf)))
        before = [k for kind, k in rec.events[:cut] if kind == "fsync"]
        assert {key(p) for p in tree} <= set(before)
        assert before.index(key(leaf / "sub" / "g")) < before.index(key(leaf / "sub"))
        assert before[-1] == key(leaf)  # the stage directory last, after everything in it
        chain = [key(base / "x" / "y"), key(base / "x"), key(base)]
        assert rec.after_rename() == chain
        assert len(synced) == len(before) + len(chain)

        rec.events.clear()  # D2: the same walk over the bound unit, then the same chain
        out.sync_chain()  # type: ignore[attr-defined]
        assert rec.synced()[-3:] == chain
        assert set(rec.synced()[:-3]) == {key(p) for p in tree} and rec.synced()[-4] == key(leaf)
        assert (leaf / UNIT_FILE).read_bytes() == UNIT_BYTES
    finally:
        if root_fd >= 0:
            os.close(root_fd)


FAULTS = ["file", "stage", "rename", "chain:y", "chain:x", "chain:anchor"]


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("policy", POLICIES)
def test_durable_publish_faults(
    base: Path, policy: str, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fileaccess = _fileaccess()
    out, root_fd = _setup(base, policy, monkeypatch)
    parent = base / "x" / "y"
    try:
        txn = out.begin("dir", "exclusive", label="publication")  # type: ignore[attr-defined]
        stage_unit(txn)
        (stage,) = parent.glob(STAGE_GLOB)
        targets = {
            "file": ("fsync", key(stage / UNIT_FILE)),
            "stage": ("fsync", key(stage)),
            "rename": ("rename", key(stage)),
            "chain:y": ("fsync", key(parent)),
            "chain:x": ("fsync", key(base / "x")),
            "chain:anchor": ("fsync", key(base)),
        }
        hit: list[object] = []

        def fail(event: tuple[str, tuple[int, int]]) -> bool:
            if event == targets[fault] and not hit:
                hit.append(event)
                return True
            return False

        Recorder(monkeypatch, fail)
        with pytest.raises(Exception) as caught:
            txn.publish()
        assert hit  # non-vacuity: the fault fired at the named step
        exc = caught.value
        text = str(exc)
        assert OSMARK not in text and str(base) not in text and "y/out" not in text
        assert stage.name not in text
        leaf = parent / "out"
        if fault.startswith("chain:"):
            assert isinstance(exc, fileaccess.CommitUncertain)  # type: ignore[attr-defined]
            assert exc.errno == errno.EIO  # type: ignore[attr-defined]
            assert (leaf / UNIT_FILE).read_bytes() == UNIT_BYTES  # bound, never removed
            txn.abort()
            assert (leaf / UNIT_FILE).read_bytes() == UNIT_BYTES and not list(
                parent.glob(STAGE_GLOB)
            )
        else:
            assert isinstance(exc, OSError) and exc.errno == errno.EIO
            assert not leaf.exists() and stage.exists()  # stage kept for abort
            txn.abort()
            assert not stage.exists() and not leaf.exists()
    finally:
        if root_fd >= 0:
            os.close(root_fd)

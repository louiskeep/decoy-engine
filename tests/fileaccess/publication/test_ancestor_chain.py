"""F22 5.0 rule (2): the durable chain reaches the anchor whoever made each directory, and an
unreadable directory is a boundary only when another uid owns it.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 rows
`test_ancestor_created_by_other_attempt` and `test_unreadable_ancestor_fail_closed` (output chain;
the vault chain is slice b1). `EACCES` is injected on the seeded directory's read open, so the
cases hold under any uid, root included.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.fileaccess.publication._chain_support import (
    UNIT_BYTES,
    UNIT_FILE,
    Recorder,
    anchor_at,
    deny_reads,
    key,
    lost_entry,
    output_dir,
    restat,
    stage_unit,
)
from tests.fileaccess.publication._dirmode_support import KILL_MARKER, publish_case, run_child

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durable publish")

POLICIES = ["strict", "ambient"]


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


def _publish(out: Any, rec: Recorder, parent: Path) -> None:
    txn = out.begin("dir", "exclusive", label="publication")
    stage_unit(txn, rec, parent)
    txn.publish()


@pytest.mark.parametrize("maker", ["killed_child", "live_begin", "foreign_mkdir"])
@pytest.mark.parametrize("policy", POLICIES)
def test_ancestor_created_by_other_attempt(
    base: Path, policy: str, maker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = base / "a"
    if maker == "killed_child":  # SIGKILL between mkdir(a) and any fsync of `base`
        spec = {"base": str(base), "policy": policy, "umask": 0o022, "rel": "a/b/out"}
        spec.update(label="publication", kill_after_mkdir="a")
        code, _ = run_child(publish_case, spec, base.parent)
        assert code == -signal.SIGKILL and (base.parent / KILL_MARKER).exists()  # non-vacuity
    elif maker == "foreign_mkdir":
        os.mkdir(a)
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    out, root_fd = output_dir(policy, base, "a/b/out")
    rec = Recorder(monkeypatch)
    fired: list[Any] = []
    if maker == "live_begin":  # another begin() makes `a` (and `b`) while this one is about to
        inner = os.mkdir

        def racing(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            if os.path.basename(os.fsdecode(path)) == "a" and not fired:
                fired.append(None)  # set first: the other begin() comes back through here
                fired[0] = out.begin("dir", "exclusive", label="publication")
            inner(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "mkdir", racing)
    else:
        assert a.is_dir()  # non-vacuity: `a` pre-existed this attempt
    try:
        _publish(out, rec, a / "b")
        if maker == "live_begin":
            assert fired  # non-vacuity: the other attempt made `a` first
            fired[0].abort()
        leaf = a / "b" / "out"
        assert (leaf / UNIT_FILE).read_bytes() == UNIT_BYTES
        assert rec.after_rename() == [key(a / "b"), key(a), key(base)]
        assert lost_entry(rec.events, [base, a, a / "b", leaf, leaf / UNIT_FILE]) is None
    finally:
        if root_fd >= 0:
            os.close(root_fd)


@pytest.mark.parametrize("policy", POLICIES)
def test_unreadable_ancestor_fail_closed_seeded(
    base: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(i) `p/a/out` with `p` a same-uid directory Decoy cannot read: never `published`."""
    from decoy_engine import fileaccess

    (base / "p" / "a").mkdir(parents=True)
    os.chmod(base / "p", 0o300)
    seen: list[str] = []
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    out, root_fd = output_dir(policy, base, "p/a/out")
    deny_reads(monkeypatch, base / "p", seen)
    leaf = base / "p" / "a" / "out"
    try:
        if policy == "strict":  # the strict chain opens `p` in begin(): refused before bind
            with pytest.raises(fileaccess.FileAccessDenied):
                out.begin("dir", "exclusive", label="publication")
            assert not leaf.exists()
        else:  # the ambient chain meets `p` after the rename: bound, not confirmed durable
            txn = out.begin("dir", "exclusive", label="publication")
            stage_unit(txn)
            with pytest.raises(fileaccess.CommitUncertain):  # type: ignore[attr-defined]
                txn.publish()
            assert (leaf / UNIT_FILE).read_bytes() == UNIT_BYTES  # kept (4.1 E13)
        assert seen  # non-vacuity: the read open of `p` was refused
    finally:
        os.chmod(base / "p", 0o700)
        if root_fd >= 0:
            os.close(root_fd)


@pytest.mark.parametrize("policy", POLICIES)
def test_unreadable_ancestor_fail_closed_create_under(
    base: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(ii) `begin()` into `q/new/out` with `q` unreadable: refused, `q` gains no entry."""
    from decoy_engine import fileaccess

    (base / "q").mkdir()
    os.chmod(base / "q", 0o300)
    seen: list[str] = []
    out, root_fd = output_dir(policy, base, "q/new/out")
    deny_reads(monkeypatch, base / "q", seen)
    try:
        with pytest.raises((fileaccess.FileAccessDenied, PermissionError)):
            out.begin("dir", "exclusive", label="publication")
        assert seen  # non-vacuity: the read open of `q` was refused
    finally:
        os.chmod(base / "q", 0o700)
        if root_fd >= 0:
            os.close(root_fd)
    assert os.listdir(base / "q") == []


def test_unreadable_ancestor_fail_closed_foreign_owner(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(iii) ambient: an unreadable directory above every created one and its parent, owned by
    another uid (owner probe patched), ends the walk and the publish succeeds."""
    from decoy_engine.fileaccess import _local

    h, u = base / "h", base / "h" / "u"
    u.mkdir(parents=True)
    seen: list[str] = []
    real = _local._probe_dir  # type: ignore[attr-defined]
    top = os.path.realpath(h)

    def probe(path: Any) -> os.stat_result:
        st = real(path)
        return restat(st, uid=os.geteuid() + 1) if os.path.realpath(path) == top else st

    monkeypatch.setattr(_local, "_probe_dir", probe)
    out, _ = output_dir("ambient", base, "h/u/w/out")
    deny_reads(monkeypatch, h, seen)
    rec = Recorder(monkeypatch)
    _publish(out, rec, u / "w")
    assert seen  # non-vacuity: the walk met the unreadable directory
    assert (u / "w" / "out" / UNIT_FILE).read_bytes() == UNIT_BYTES
    assert rec.after_rename() == [key(u / "w"), key(u)]

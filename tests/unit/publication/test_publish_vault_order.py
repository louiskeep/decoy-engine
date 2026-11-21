"""F22 5.5 rule V inside `publish_unit` (slice b2): no unit with a vault is bound before its vault
is durable, and a republish needs the descriptor's vault digest on disk and this writer's rows.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 rows `test_vault_order_is_structural`,
`test_republish_requires_vault_digest` and `test_two_writers_same_output_different_vault`. Faults
are armed in the `_after_judge` hook, so each one lands in the vault or bind step it names and
never in the staging that came before it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine import vault as vault_module
from decoy_engine.vault import VaultError
from tests.fileaccess.publication._chain_support import Recorder, anchor_at, key
from tests.fileaccess.publication._dirmode_support import POLICIES
from tests.fileaccess.publication._vault_support import OTHER_ROWS, ROWS, vault_out, writer
from tests.unit.publication._publish_support import snapshot, stages_left
from tests.unit.publication._vault_publish_support import (
    ALICE,
    BOB,
    OUT_REL,
    VAULT_REL,
    bound_descriptor,
    close_all,
    prepare,
    publish,
    sha,
    start_winner,
    vault_fingerprint,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durable vault")
MISMATCH = "publication_vault_mismatch"
V2_FIXTURE = Path(__file__).parents[1] / "vault" / "fixtures" / "decoy-vault-v2__retired.vault"
CONFLICT = "publication_vault_conflict"
COMMIT_FAILED = reasons.runtime.VAULT_COMMIT_FAILED


def _root(tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch, name: str = "root") -> Path:
    base = tmp_path / name
    prepare(base)
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    return base


def _eio() -> OSError:
    return OSError(errno.EIO, os.strerror(errno.EIO))


class _Order:
    """Logs fsync, the vault `link` and the stage rename by `(st_dev, st_ino)`, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rec = Recorder(monkeypatch)
        real_link = os.link

        def link(src: Any, dst: Any, **kw: Any) -> None:
            real_link(src, dst, **kw)
            st = os.stat(dst, dir_fd=kw.get("dst_dir_fd"), follow_symlinks=False)
            self.rec.events.append(("link", (st.st_dev, st.st_ino)))

        monkeypatch.setattr(os, "link", link)

    def index(self, kind: str, target: Any, after: int = -1) -> int:
        for i, event in enumerate(self.rec.events):
            if i > after and event == (kind, target):
                return i
        raise AssertionError(f"{kind} not seen")


@pytest.mark.parametrize("policy", POLICIES)
def test_vault_order_is_structural(
    tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _root(tmp_path, policy, monkeypatch)
    leaf, vleaf = base / OUT_REL, base / VAULT_REL
    order = _Order(monkeypatch)
    receipt, fds = publish(policy, base, "one", "op-1")
    close_all(fds)
    assert (receipt.outcome, receipt.durable) == ("published", True)
    linked = order.index("link", key(vleaf))
    renamed = order.index("rename", key(leaf))
    vault_dir = order.index("fsync", key(base / "vaults"), after=linked)
    anchor = order.index("fsync", key(base), after=vault_dir)
    assert linked < vault_dir < anchor < renamed  # the vault chain is durable before the bind
    bound = bound_descriptor(leaf)
    assert bound is not None and bound["vault"]["sha256"] == sha(vleaf) == receipt.vault_digest
    assert bound["vault"]["size"] == vleaf.stat().st_size and bound["vault"]["entry_count"] == 2


def _fault(case: str, base: Path, monkeypatch: pytest.MonkeyPatch, fired: list[str]) -> None:
    """Arm `case` in the `_after_judge` hook: it fires in the vault step, never in staging."""
    from decoy_engine.publication import _publish

    armed = {"on": False}
    monkeypatch.setattr(_publish, "_after_judge", lambda: armed.update(on=True))
    real_write, real_link, real_fsync = os.write, os.link, os.fsync
    vaults, anchor = key(base / "vaults"), key(base)

    def write(fd: int, data: Any) -> int:
        if armed["on"] and case == "stage_write":
            fired.append(case)
            raise _eio()
        return real_write(fd, data)

    def link(*args: Any, **kw: Any) -> None:
        if armed["on"] and case == "link":
            fired.append(case)
            raise _eio()
        real_link(*args, **kw)

    def fsync(fd: int) -> None:
        st = os.fstat(fd)
        hit = {
            "file_fsync": stat.S_ISREG(st.st_mode) and not fired,
            "dir_fsync": (st.st_dev, st.st_ino) == vaults,
            "chain_fsync": (st.st_dev, st.st_ino) == anchor,
        }
        if armed["on"] and hit.get(case):
            fired.append(case)
            raise _eio()
        real_fsync(fd)

    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "fsync", fsync)


@pytest.mark.parametrize(
    "case, leaf_kept",
    [
        ("stage_write", False),
        ("file_fsync", False),
        ("link", False),
        ("dir_fsync", True),  # B-6 rule 7: a linked vault is kept, never removed
        ("chain_fsync", True),
    ],
)
@pytest.mark.parametrize("policy", POLICIES)
def test_vault_order_is_structural_under_faults(
    tmp_path: Path, policy: str, case: str, leaf_kept: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _root(tmp_path, policy, monkeypatch)
    leaf, vleaf = base / OUT_REL, base / VAULT_REL
    fired: list[str] = []
    _fault(case, base, monkeypatch, fired)
    with pytest.raises(VaultError) as caught:
        publish(policy, base, "one", "op-1")
    assert fired and fired[0] == case  # non-vacuity: the fault fired in the vault step
    assert caught.value.code == COMMIT_FAILED
    assert not leaf.exists()  # the output is never bound
    assert vleaf.exists() is leaf_kept
    assert stages_left(base / "a") == []


@pytest.mark.parametrize("policy", POLICIES)
def test_vault_exists_with_other_rows_is_never_bound(
    tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _root(tmp_path, policy, monkeypatch)
    leaf, vleaf = base / OUT_REL, base / VAULT_REL
    vout, vfd = vault_out(policy, base, VAULT_REL)
    writer(OTHER_ROWS).publish(vout)
    close_all([vfd])
    before = vault_fingerprint(vleaf)
    assert before is not None  # non-vacuity: a vault is at the leaf
    receipt, fds = publish(policy, base, "one", "op-1")
    close_all(fds)
    assert (receipt.outcome, receipt.reason_code) == ("refused", CONFLICT)
    assert (receipt.visible, receipt.durable, receipt.vault_digest) == (False, False, None)
    assert not leaf.exists() and vault_fingerprint(vleaf) == before
    assert stages_left(base / "a") == []


@pytest.mark.parametrize("kind", ["retired_v2", "tampered_v3"])
@pytest.mark.parametrize("policy", POLICIES)
def test_vault_exists_as_v2_or_tampered_is_never_bound(
    tmp_path: Path, policy: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaf this engine cannot read as its own (a retired v2 file, or a v3 file with one
    flipped byte) is a conflict: the unit is refused and the file is left as it was."""
    base = _root(tmp_path, policy, monkeypatch)
    leaf, vleaf = base / OUT_REL, base / VAULT_REL
    if kind == "retired_v2":
        vleaf.write_bytes(V2_FIXTURE.read_bytes())
    else:
        blob = bytearray(writer().serialize()[0])
        blob[-1] ^= 0xFF
        vleaf.write_bytes(bytes(blob))
    before = vault_fingerprint(vleaf)
    receipt, fds = publish(policy, base, "one", "op-1")
    close_all(fds)
    assert (receipt.outcome, receipt.reason_code) == ("refused", CONFLICT)
    assert not leaf.exists() and vault_fingerprint(vleaf) == before
    assert stages_left(base / "a") == []


@pytest.mark.parametrize("policy", POLICIES)
def test_vault_exists_with_the_same_rows_is_adopted(
    tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault a killed attempt linked is adopted in place, never rewritten (5.5 step 2)."""
    base = _root(tmp_path, policy, monkeypatch)
    leaf, vleaf = base / OUT_REL, base / VAULT_REL
    vout, vfd = vault_out(policy, base, VAULT_REL)
    writer(ROWS).publish(vout)
    close_all([vfd])
    before = vault_fingerprint(vleaf)
    receipt, fds = publish(policy, base, "one", "op-1")
    close_all(fds)
    assert (receipt.outcome, receipt.durable) == ("published", True)
    assert before is not None and vault_fingerprint(vleaf) == before
    bound = bound_descriptor(leaf)
    assert bound is not None and bound["vault"]["sha256"] == before[0] == receipt.vault_digest


REPUBLISH = {
    "untouched": ("already_committed", None),
    "deleted": ("refused", MISMATCH),
    "other_rows": ("refused", MISMATCH),
    "same_rows_rewritten": ("refused", MISMATCH),  # equal rows, other bytes: the digest decides
    "request_without_vault": ("refused", MISMATCH),
    "bound_without_vault": ("refused", MISMATCH),
    "fsync_eio": ("commit_uncertain", "publication_commit_uncertain"),
}


@pytest.mark.parametrize("case", sorted(REPUBLISH))
@pytest.mark.parametrize("policy", POLICIES)
def test_republish_requires_vault_digest(
    tmp_path: Path, policy: str, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _root(tmp_path, policy, monkeypatch)
    leaf, vleaf = base / OUT_REL, base / VAULT_REL
    first, fds = publish(policy, base, "one", "op-1", vault=case != "bound_without_vault")
    close_all(fds)
    assert first.outcome == "published"
    if case in ("deleted", "other_rows", "same_rows_rewritten"):
        vleaf.unlink()
    if case in ("other_rows", "same_rows_rewritten"):
        vout, vfd = vault_out(policy, base, VAULT_REL)
        writer(OTHER_ROWS if case == "other_rows" else ROWS).publish(vout)
        close_all([vfd])
    unit, vault_before = snapshot(leaf), vault_fingerprint(vleaf)
    listing = sorted(os.listdir(base / "vaults"))
    published: list[bool] = []
    real_publish = vault_module.VaultWriter.publish

    def spy(self: Any, out: Any) -> Any:
        published.append(True)
        return real_publish(self, out)

    monkeypatch.setattr(vault_module.VaultWriter, "publish", spy)
    if case == "fsync_eio":
        Recorder(monkeypatch, fail=lambda e: e == ("fsync", key(vleaf)))
    receipt, fds = publish(policy, base, "one", "op-1", vault=case != "request_without_vault")
    close_all(fds)
    assert (receipt.outcome, receipt.reason_code) == REPUBLISH[case]
    assert receipt.result_id == first.result_id  # non-vacuity: same result, so D3 decided
    if case == "untouched":
        assert receipt.durable is True and receipt.vault_digest == sha(vleaf)
    if case == "fsync_eio":
        assert (receipt.visible, receipt.durable, receipt.os_error) == (True, False, "EIO")
    assert snapshot(leaf) == unit and vault_fingerprint(vleaf) == vault_before
    assert sorted(os.listdir(base / "vaults")) == listing and published == []
    assert stages_left(base / "a") == []


def _hooked_loser(
    tmp_path: Path, policy: str, base: Path, l_op: str, monkeypatch: pytest.MonkeyPatch
) -> Any:
    spec = {"base": str(base), "policy": policy, "op": "op-1"}
    proc, result_path, w_in_hook, l_probed, l_waiting = start_winner(spec, tmp_path)
    try:
        assert w_in_hook.wait(60), "winner never reached the hook"
        probe = os.open(base / "a" / ".out.decoy-lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):  # non-vacuity: W holds the lock in the hook
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        l_probed.set()
        real_flock = fcntl.flock

        def flock(fd: int, op: int) -> None:
            try:
                real_flock(fd, op)
            except BlockingIOError:
                l_waiting.set()
                raise

        monkeypatch.setattr(fcntl, "flock", flock)
        receipt, fds = publish(policy, base, "one", l_op, BOB)
        close_all(fds)
        monkeypatch.setattr(fcntl, "flock", real_flock)
    finally:
        l_probed.set()
        l_waiting.set()
        proc.join(120)
    assert proc.exitcode == 0
    w_result = json.loads(result_path.read_text("utf-8"))
    assert w_result == {"outcome": "published", "fired": True}  # non-vacuity: W was parked
    return receipt


@pytest.mark.parametrize("mode", ["sequential", "hook"])
@pytest.mark.parametrize("l_op", ["op-1", "op-2"])
@pytest.mark.parametrize("policy", POLICIES)
def test_two_writers_same_output_different_vault(
    tmp_path: Path, policy: str, l_op: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W binds tables T with X reversing to Alice; L has the same T with X reversing to Bob."""
    base = _root(tmp_path, policy, monkeypatch)
    leaf, vleaf = base / OUT_REL, base / VAULT_REL
    if mode == "sequential":
        won, fds = publish(policy, base, "one", "op-1", ALICE)
        close_all(fds)
        assert won.outcome == "published"
        before = vault_fingerprint(vleaf)
        receipt, fds = publish(policy, base, "one", l_op, BOB)
        close_all(fds)
    else:
        receipt = _hooked_loser(tmp_path, policy, base, l_op, monkeypatch)
        before = vault_fingerprint(vleaf)
    bound = bound_descriptor(leaf)
    assert bound is not None and receipt.result_id == bound["result_id"]  # equal output
    assert (receipt.outcome, receipt.reason_code) == ("refused", MISMATCH)
    assert before is not None and vault_fingerprint(vleaf) == before
    unit = snapshot(leaf)
    again, fds = publish(policy, base, "one", "op-1", ALICE)  # non-vacuity: W's rows still match
    close_all(fds)
    assert (again.outcome, again.durable) == ("already_committed", True)
    assert snapshot(leaf) == unit and vault_fingerprint(vleaf) == before
    assert stages_left(base / "a") == []

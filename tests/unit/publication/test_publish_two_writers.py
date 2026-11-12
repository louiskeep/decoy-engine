"""F22 5.3, 7.1: two writers on one destination, ordered by the `_after_judge` hook (slice a3).

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 rows
`test_two_writers_different_result` and `test_two_writers_same_result`. Winner W (a `spawn`
child) parks in `_after_judge` with the lock held and a bind verdict, sets `w_in_hook` and waits
on `l_probed`. Loser L (this process) waits on `w_in_hook`, must see `EWOULDBLOCK` from a
`flock(LOCK_EX|LOCK_NB)` probe of the lock file (non-vacuity), sets `l_probed` and calls
`publish_unit`. W binds only once L is itself blocked on the lock (`l_waiting`, set by a spy on
L's first refused `flock`), so a judge taken before the lock sees no binding on every run.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.unit.publication._publish_support import (
    POLICIES,
    close_root,
    descriptor,
    make_out,
    snapshot,
    stages_left,
    start_winner,
    tables,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock")


def _race(
    tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch, *, w: dict, l_seed: str, l_op: str
) -> tuple[Any, dict, Path]:
    from decoy_engine.publication import PublicationRequest, publish_tables

    base = tmp_path / "root"
    (base / "a").mkdir(parents=True)
    leaf = base / "a" / "out"
    spec = {"base": str(base), "policy": policy, "rel": "a/out", **w}
    proc, result_path, w_in_hook, l_probed, l_waiting = start_winner(spec, tmp_path)
    out, root_fd = make_out(policy, base, "a/out")
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
        receipt = publish_tables(out, tables(l_seed), PublicationRequest(l_op, lock_timeout_s=60))
        monkeypatch.setattr(fcntl, "flock", real_flock)
    finally:
        l_probed.set()
        l_waiting.set()
        proc.join(120)
        close_root(root_fd)
    assert proc.exitcode == 0
    w_result = json.loads(result_path.read_text("utf-8"))
    assert w_result["fired"] is True  # non-vacuity: W was parked in the hook
    return receipt, w_result, leaf


@pytest.mark.parametrize(
    "l_op, outcome, code",
    [
        ("op-1", "operation_conflict", "publication_operation_conflict"),
        ("op-2", "refused", "publication_target_occupied"),
    ],
)
@pytest.mark.parametrize("policy", POLICIES)
def test_two_writers_different_result(
    tmp_path: Path,
    policy: str,
    l_op: str,
    outcome: str,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, w_result, leaf = _race(
        tmp_path, policy, monkeypatch, w={"seed": "one", "op": "op-1"}, l_seed="two", l_op=l_op
    )
    assert w_result["outcome"] == "published"
    assert (receipt.outcome, receipt.reason_code) == (outcome, code)
    bound = descriptor(leaf)
    assert bound["operation_id"] == "op-1" and receipt.result_id != bound["result_id"]
    assert sorted(os.listdir(leaf)) == ["_decoy_publication.json", "customers.parquet"]
    assert snapshot(leaf)["entries"]  # exactly one binding, the winner's
    assert stages_left(leaf.parent) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_two_writers_same_result(
    tmp_path: Path, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, w_result, leaf = _race(
        tmp_path, policy, monkeypatch, w={"seed": "one", "op": "op-1"}, l_seed="one", l_op="op-1"
    )
    assert w_result["outcome"] == "published"
    assert (receipt.outcome, receipt.reason_code, receipt.durable) == (
        "already_committed",
        None,
        True,
    )
    assert descriptor(leaf)["result_id"] == receipt.result_id
    assert sorted(os.listdir(leaf)) == ["_decoy_publication.json", "customers.parquet"]
    assert [p.name for p in leaf.parent.iterdir() if p.name.startswith("out")] == ["out"]
    assert stages_left(leaf.parent) == []

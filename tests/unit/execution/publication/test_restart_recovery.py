"""`test_restart_recovery` and `test_restart_same_operation_different_result`: a sink commits
through `publish_unit`, so a restart is judged by its measured result, and only a sink that
measured its own result can be sealed by `reconcile()` (plan 5.6, 7.2; slice c1b; G1a, G2, G3).

Red before c1b (start commit `acb2c4aa`): the sink takes no `request` and has no `reconcile()`;
its commit is a plain `table_sink` rename, so the fresh sink of a restart fails with `OSError`
39 (probe P1) instead of `already_committed`, and nothing is ever UNCERTAIN.
"""

from __future__ import annotations

import errno
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.fileaccess import CommitUncertain
from decoy_engine.publication import PublicationRefused, PublicationRequest
from decoy_engine.publication._sink_state import SinkState
from decoy_engine.reasons import PublicationState
from tests.fileaccess.publication._chain_support import Recorder, anchor_at, key
from tests.unit.execution.publication._sink_support import (
    DESCRIPTOR,
    POLICIES,
    sink_crash_child,
    sink_factory,
    stages,
    table,
)
from tests.unit.publication._publish_support import KILL_MARKER, run_spec, snapshot

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durable publish")

S = SinkState


def _refused(call: Any) -> PublicationRefused:
    with pytest.raises(PublicationRefused) as info:
        call()
    return info.value


def _unit_keys(leaf: Path) -> set[tuple[int, int]]:
    return {key(p) for p in leaf.rglob("*")}


def _fail_after_rename(rec_box: list[Recorder], target: tuple[int, int]) -> Any:
    """An fsync failure on `target`, only once the bind rename happened (not before it)."""

    def fail(event: tuple[str, tuple[int, int]]) -> bool:
        renamed = any(kind == "rename" for kind, _ in rec_box[0].events) if rec_box else False
        return event == ("fsync", target) and renamed

    return fail


def _uncertain_sink(make: Any, base: Path, patch: pytest.MonkeyPatch) -> Any:
    """A sink whose commit bound the unit and then failed the parent fsync (the commit point)."""
    box: list[Recorder] = []
    box.append(Recorder(patch, fail=_fail_after_rename(box, key(base))))
    sink = make(PublicationRequest("op-1"))
    sink.write("customers", table("A"))
    with pytest.raises(CommitUncertain):
        sink.commit()
    return sink


@pytest.mark.parametrize("policy", POLICIES)
def test_restart_recovery(policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child commits through a sink and is SIGKILLed after the bind rename. A fresh sink with
    the same `operation_id` and tables gets `already_committed` from `commit()` only after the
    unit tree and its chain are fsynced (D2), and is sealed."""
    base = tmp_path / "root"
    base.mkdir()
    spec = {"base": str(base), "policy": policy, "rel": "out", "seed": "A", "op": "op-1"}
    code, _ = run_spec(sink_crash_child, spec, tmp_path)
    assert code == -signal.SIGKILL
    assert (tmp_path / KILL_MARKER).exists()  # non-vacuity: killed after the rename
    leaf = base / "out"
    assert (leaf / DESCRIPTOR).is_file()
    before = snapshot(leaf)

    if policy == "ambient":
        anchor_at(monkeypatch, base)
    with sink_factory(policy, base) as make:
        sink = make(PublicationRequest("op-1"))
        assert sink.reconcile() is None  # OPEN: nothing measured, nothing sealed
        assert sink.state is S.OPEN
        sink.write("customers", table("A"))
        with monkeypatch.context() as patch:
            rec = Recorder(patch)
            receipt = sink.commit()
        assert (receipt.outcome, receipt.durable, receipt.visible) == (
            "already_committed",
            True,
            True,
        )
        synced = rec.synced()
        assert _unit_keys(leaf) <= set(synced)
        assert [k for k in synced if k in (key(leaf), key(base))] == [key(leaf), key(base)]
        assert sink.state is S.SEALED
        assert sink.receipt == receipt
        assert sink.publication_evidence.state is PublicationState.PUBLISHED
        assert sink.commit() == receipt
        refusal = _refused(lambda: sink.write("orders", table("B")))
        assert refusal.code == "publication_attempt_sealed"
    assert snapshot(leaf) == before
    assert stages(base) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_restart_recovery_reconcile_seals_an_uncertain_sink_after_d4(
    policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COMMITTING, `CommitUncertain` (bound, the parent fsync failed): UNCERTAIN, and every call
    but reconcile is refused. `reconcile()` with the fsync still failing stays UNCERTAIN; once
    it passes, D4 fsyncs the unit tree and the chain before the sink is sealed."""
    base = tmp_path / "root"
    base.mkdir()
    if policy == "ambient":
        anchor_at(monkeypatch, base)
    leaf = base / "out"
    with sink_factory(policy, base) as make:
        with monkeypatch.context() as patch:
            sink = _uncertain_sink(make, base, patch)
            assert sink.state is S.UNCERTAIN
            uncertain = sink.receipt
            assert (uncertain.outcome, uncertain.visible, uncertain.durable) == (
                "commit_uncertain",
                True,
                False,
            )
            assert sink.publication_evidence.state is PublicationState.UNKNOWN
            assert (leaf / DESCRIPTOR).is_file()  # non-vacuity: the rename bound the unit
            for call in (sink.commit, lambda: sink.write("orders", table("B"))):
                assert _refused(call).code == "publication_commit_uncertain"
            with pytest.raises(CommitUncertain):
                sink.reconcile()  # D4 fsync still fails: still uncertain
            assert sink.state is S.UNCERTAIN
        before = snapshot(leaf)
        with monkeypatch.context() as patch:
            rec = Recorder(patch)
            sealed = sink.reconcile()
        assert sealed is not None
        assert (sealed.outcome, sealed.durable, sealed.reason_code) == (
            "already_committed",
            True,
            None,
        )
        assert sealed.result_id == uncertain.result_id
        synced = rec.synced()
        assert _unit_keys(leaf) <= set(synced)
        assert [k for k in synced if k in (key(leaf), key(base))] == [key(leaf), key(base)]
        assert sink.state is S.SEALED
        assert sink.publication_evidence.state is PublicationState.PUBLISHED
        assert sink.reconcile() == sealed
        assert sink.commit() == sealed
        sink.abort()
        assert sink.state is S.SEALED
    assert snapshot(leaf) == before


@pytest.mark.parametrize("case", ["absent", "different"])
@pytest.mark.parametrize("policy", POLICIES)
def test_restart_recovery_reconcile_refuses_a_unit_that_is_not_its_own(
    policy: str, case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RECOVERING, absent or a different result: REFUSED `publication_commit_not_visible`."""
    base = tmp_path / "root"
    base.mkdir()
    leaf = base / "out"
    with sink_factory(policy, base) as make:
        with monkeypatch.context() as patch:
            sink = _uncertain_sink(make, base, patch)
        shutil.rmtree(leaf)  # the bound unit removed out of band
        if case == "different":
            other = make(PublicationRequest("op-1"))
            other.write("customers", table("B"))
            assert other.commit().outcome == "published"
            assert other.receipt.result_id != sink.receipt.result_id  # non-vacuity
        refusal = _refused(sink.reconcile)
        assert refusal.code == "publication_commit_not_visible"
        assert refusal.receipt.visible is False
        assert sink.state is S.REFUSED
        assert sink.publication_evidence.state is PublicationState.NOT_PUBLISHED
        assert _refused(sink.reconcile).code == "publication_commit_not_visible"
    assert stages(base) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_restart_recovery_reconcile_eio_reading_the_unit_stays_uncertain(
    policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RECOVERING cannot read what is bound: it cannot tell, so the sink stays UNCERTAIN."""
    base = tmp_path / "root"
    base.mkdir()
    with sink_factory(policy, base) as make:
        with monkeypatch.context() as patch:
            sink = _uncertain_sink(make, base, patch)
        out = sink._out

        def unreadable(*_a: Any, **_k: Any) -> bytes:
            raise OSError(errno.EIO, "injected")

        monkeypatch.setattr(out, "read_member", unreadable)
        with pytest.raises(CommitUncertain):
            sink.reconcile()
        assert sink.state is S.UNCERTAIN
        assert sink.receipt.os_error == "EIO"


@pytest.mark.parametrize("policy", POLICIES)
def test_restart_same_operation_different_result(policy: str, tmp_path: Path) -> None:
    """A unit bound for `op-1` with result A; a fresh sink with `op-1` writes B: `reconcile()`
    seals nothing, `commit()` is `operation_conflict` (REFUSED), A is untouched, and no receipt
    for A is returned."""
    base = tmp_path / "root"
    base.mkdir()
    leaf = base / "out"
    with sink_factory(policy, base) as make:
        first = make(PublicationRequest("op-1"))
        first.write("customers", table("A"))
        bound = first.commit()
        assert bound.outcome == "published"
        before = snapshot(leaf)
        fresh = make(PublicationRequest("op-1"))
        assert fresh.reconcile() is None  # never adopts a unit on `operation_id` alone
        assert fresh.state is S.OPEN
        fresh.write("customers", table("B"))
        refusal = _refused(fresh.commit)
        receipt = refusal.receipt
        assert (receipt.outcome, refusal.code) == (
            "operation_conflict",
            "publication_operation_conflict",
        )
        assert receipt.result_id is not None
        assert receipt.result_id != bound.result_id  # non-vacuity: B measured, and B is not A
        assert receipt.descriptor_digest is None and receipt.visible is False
        assert fresh.receipt == receipt
        assert fresh.state is S.REFUSED
        assert fresh.publication_evidence.state is PublicationState.NOT_PUBLISHED
        assert _refused(fresh.reconcile).code == "publication_operation_conflict"
        fresh.abort()
        assert fresh.state is S.ABORTED
    assert snapshot(leaf) == before
    assert stages(base) == []

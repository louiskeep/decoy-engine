"""F22 r5 HIGH 3: the sink's state machine is atomic under concurrent calls on ONE sink.

Each test forces one interleaving with events (never sleeps as synchronization): a blocking
hook parks one thread at the exact point the race needs, the other thread acts, then the hook
is released. Outcomes are asserted as invariants of the machine, not as timing.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution import _transactional_sink as ts
from decoy_engine.execution._transactional_sink import (
    ParquetPreparationSink,
    ParquetTransactionalSink,
)
from decoy_engine.publication import _local_directory as ld
from tests.unit.publication import _support as sup

FAIL = [pub.CheckResult("fk", "fail")]
WAIT = 5.0


def _req(dest: Path, **kw: Any) -> pub.PublicationRequest:
    return pub.PublicationRequest(destination=dest, operation_id="op-1", **kw)


class Runner:
    """A thread that records the result or exception of one call."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.value: Any = None
        self.error: BaseException | None = None
        self._t = threading.Thread(target=self._run, args=(fn,), daemon=True)

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            self.value = fn()
        except BaseException as exc:  # noqa: BLE001
            self.error = exc

    def start(self) -> Runner:
        self._t.start()
        return self

    def join(self, timeout: float = WAIT) -> Runner:
        self._t.join(timeout)
        assert not self._t.is_alive(), "thread did not finish (deadlock or blocked call)"
        return self

    def alive(self) -> bool:
        return self._t.is_alive()


def _reason(err: BaseException | None) -> str | None:
    assert isinstance(err, pub.PublicationRefusedError), repr(err)
    return err.receipt.reason_code if err.receipt else None


def _park_in(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> tuple[threading.Event, threading.Event]:
    """Wrap owner.name so its first call announces `reached` and waits for `release`."""
    reached, release = threading.Event(), threading.Event()
    real = getattr(owner, name)
    first = threading.Event()

    def wrapper(*a: Any, **k: Any) -> Any:
        if not first.is_set():
            first.set()
            reached.set()
            assert release.wait(WAIT), "never released"
        return real(*a, **k)

    monkeypatch.setattr(owner, name, wrapper)
    return reached, release


def _sink(tmp_path: Path, **kw: Any) -> tuple[ParquetTransactionalSink, Path]:
    dest = tmp_path / "out"
    sink = ParquetTransactionalSink(dest, request=_req(dest, **kw))
    sink.write("alpha", sup.table_a())
    return sink, dest


# ---------------------------------------------- race 1: abort() between the bind and the flag


def test_abort_after_the_bind_but_before_the_uncertain_flag_cannot_poison_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink, dest = _sink(tmp_path)
    real = ld.fsync_dir
    reached, release = threading.Event(), threading.Event()

    def fsync(p: Any) -> None:
        if Path(p) == dest.parent:  # the post-rename parent sync: the binding is visible now
            reached.set()
            assert release.wait(WAIT)
            raise OSError(5, "sync failed")
        real(p)

    monkeypatch.setattr(ld, "fsync_dir", fsync)
    commit = Runner(sink.commit).start()
    assert reached.wait(WAIT)
    Runner(sink.abort).start().join()  # abort lands in the bind -> flag window
    release.set()
    commit.join()
    assert isinstance(commit.error, pub.CommitUncertainError)
    monkeypatch.undo()
    receipt = sink.reconcile()  # old code: refuses `attempt_aborted`
    assert receipt is not None and receipt.outcome == "already_committed"
    sup.oracle(dest)


def test_abort_during_commit_does_not_remove_staging_out_from_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink, dest = _sink(tmp_path)
    reached, release = _park_in(monkeypatch, pub, "prepare_directory")
    commit = Runner(sink.commit).start()
    assert reached.wait(WAIT)
    Runner(sink.abort).start().join()
    release.set()
    commit.join()
    assert commit.error is None and commit.value.outcome == "published"
    sup.oracle(dest)
    assert sup.staging_dirs(tmp_path) == []


def test_abort_requested_inside_the_cancel_callback_is_honoured_after_the_refusal(
    tmp_path: Path,
) -> None:
    holder: list[ParquetTransactionalSink] = []

    def cancelled() -> bool:
        holder[0].abort()  # the realistic re-entrant call: a cancel handler aborting its sink
        return True

    sink, dest = _sink(tmp_path, cancelled=cancelled)
    holder.append(sink)
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sink.commit()
    assert exc.value.receipt.reason_code == "cancelled"
    assert sup.staging_dirs(tmp_path) == [] and not dest.exists()
    with pytest.raises(pub.PublicationRefusedError):
        sink.commit()


# ---------------------------------------------- race 2: record_validation(FAIL) vs commit


def test_record_validation_cannot_install_a_failing_request_after_the_commit_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink, dest = _sink(tmp_path)
    parked, resume = threading.Event(), threading.Event()
    real_replace = dataclasses.replace
    main = threading.get_ident()

    def replace(obj: Any, /, **changes: Any) -> Any:
        if isinstance(obj, pub.PublicationRequest) and threading.get_ident() != main and "checks" in changes:
            parked.set()
            resume.wait(1.0)  # let the commit run to completion if nothing serializes the two
        return real_replace(obj, **changes)

    monkeypatch.setattr(dataclasses, "replace", replace)
    validation = Runner(lambda: sink.record_validation(FAIL)).start()
    assert parked.wait(WAIT)
    commit = Runner(sink.commit).start()
    commit._t.join(0.5)  # parked on the sink's serialization, or already running
    resume.set()
    validation.join()
    commit.join()
    published = commit.error is None
    if published:
        # the sink never claims a validation the published unit does not carry
        assert sink.request.checks == ()
        assert isinstance(validation.error, pub.PublicationRefusedError)
        sup.oracle(dest)
    else:
        assert _reason(commit.error) == "required_check_failed"
        assert not dest.exists()


def test_record_validation_is_refused_while_a_commit_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink, dest = _sink(tmp_path)
    reached, release = _park_in(monkeypatch, pub, "prepare_directory")
    commit = Runner(sink.commit).start()
    assert reached.wait(WAIT)
    late = Runner(lambda: sink.record_validation(FAIL)).start().join()
    release.set()
    commit.join()
    assert isinstance(late.error, pub.PublicationRefusedError)
    assert commit.value.outcome == "published" and sink.request.checks == ()
    sup.oracle(dest)


# ---------------------------------------------- race 3: two commit() calls


def test_two_concurrent_commits_publish_once_and_the_loser_gets_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink, dest = _sink(tmp_path)
    reached, release = _park_in(monkeypatch, pub, "prepare_directory")
    first = Runner(sink.commit).start()
    assert reached.wait(WAIT)
    second = Runner(sink.commit).start()
    second.join(2.0)  # old code: parks inside prepare_directory too and never returns here
    assert _reason(second.error) == "commit_in_progress"
    release.set()
    first.join()
    assert first.error is None and first.value.outcome == "published"
    assert sink.commit() is first.value  # and the machine is not poisoned by the refusal
    sup.oracle(dest)


def test_a_refused_concurrent_commit_does_not_latch_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink, dest = _sink(tmp_path)
    reached, release = _park_in(monkeypatch, pub, "prepare_directory")
    first = Runner(sink.commit).start()
    assert reached.wait(WAIT)
    Runner(sink.commit).start().join(2.0)
    assert sink._reason is None  # not terminal: the in-flight commit still decides
    release.set()
    first.join()
    assert first.value.outcome == "published"


# ---------------------------------------------- writers vs abort / commit


def test_abort_during_a_write_is_deferred_until_the_write_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "out"
    sink = ParquetTransactionalSink(dest, request=_req(dest))
    sink.write("alpha", sup.table_a())
    reached, release = _park_in(monkeypatch, pq, "write_table")
    writing = Runner(lambda: sink.write("beta", sup.table_b())).start()
    assert reached.wait(WAIT)
    Runner(sink.abort).start().join()
    release.set()
    writing.join()
    assert writing.error is None  # old code: staging deleted under the writer -> OSError
    assert sup.staging_dirs(tmp_path) == []  # the deferred abort ran when the write ended
    with pytest.raises(pub.PublicationRefusedError):
        sink.commit()
    assert not dest.exists()


def test_commit_is_refused_while_a_write_is_in_flight_and_works_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "out"
    sink = ParquetTransactionalSink(dest, request=_req(dest))
    sink.write("alpha", sup.table_a())
    reached, release = _park_in(monkeypatch, pq, "write_table")
    writing = Runner(lambda: sink.write("beta", sup.table_b())).start()
    assert reached.wait(WAIT)
    early = Runner(sink.commit).start().join(2.0)
    assert _reason(early.error) == "write_in_progress"
    release.set()
    writing.join()
    receipt = sink.commit()
    assert receipt.outcome == "published"
    assert {a.role for a in receipt.artifacts} >= {"table"}
    sup.oracle(dest)


def test_concurrent_first_writes_create_exactly_one_staging_directory(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    sink = ParquetTransactionalSink(dest, request=_req(dest))
    gate = threading.Barrier(6, timeout=WAIT)

    def go(i: int) -> Runner:
        def body() -> None:
            gate.wait()
            sink.write(f"t{i}", sup.table_a())

        return Runner(body).start()

    runners = [go(i) for i in range(6)]
    for r in runners:
        r.join()
    assert all(r.error is None for r in runners)
    assert len(sup.staging_dirs(tmp_path)) == 1
    assert sink.commit().outcome == "published"
    assert len(list(dest.glob("t*.parquet"))) == 6


# ---------------------------------------------- the preparation sink shares the machine


def test_two_concurrent_preparation_commits_seal_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "child"
    sink = ParquetPreparationSink(target)
    sink.write("t", sup.table_a())
    real = ts.os.replace
    reached, release = threading.Event(), threading.Event()
    seen = {"n": 0}

    def replace(src: Any, dst: Any) -> None:
        seen["n"] += 1
        if seen["n"] == 1:
            reached.set()
            assert release.wait(WAIT)
        real(src, dst)

    monkeypatch.setattr(ts.os, "replace", replace)
    first = Runner(sink.commit).start()
    assert reached.wait(WAIT)
    second = Runner(sink.commit).start().join(2.0)
    assert isinstance(second.error, pub.PublicationRefusedError)
    release.set()
    first.join()
    assert first.error is None and seen["n"] == 1
    assert (target / "t.parquet").exists()


# ---------------------------------------------- stress: invariants, any interleaving


@pytest.mark.parametrize("round_", range(12))
def test_random_interleavings_never_break_the_machine(tmp_path: Path, round_: int) -> None:
    dest = tmp_path / "out"
    sink = ParquetTransactionalSink(dest, request=_req(dest))
    sink.write("alpha", sup.table_a())
    gate = threading.Barrier(6, timeout=WAIT)
    ops: list[Callable[[], Any]] = [
        sink.commit,
        sink.commit,
        sink.abort,
        lambda: sink.record_validation(FAIL),
        lambda: sink.write("beta", sup.table_b()),
        sink.reconcile,
    ]
    runners = []
    for op in ops:
        def body(op: Callable[[], Any] = op) -> Any:
            gate.wait()
            return op()

        runners.append(Runner(body).start())
    for r in runners:
        r.join()
    for r in runners:
        assert r.error is None or isinstance(r.error, pub.CommitError), repr(r.error)
    published = dest.exists()
    if published:
        sup.oracle(dest)
        assert sink.receipt is not None or sink._uncertain
    assert sup.staging_dirs(tmp_path) == [] or not published
    sink.abort()
    assert sup.staging_dirs(tmp_path) == []

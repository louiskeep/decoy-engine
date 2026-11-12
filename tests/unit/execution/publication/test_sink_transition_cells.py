"""`test_sink_transition_cells`, sink half: the 5.6 cells `ParquetTransactionalSink` reaches
today (plan 5.6, 7.2; class 3), on both POSIX policies.

Red before c1a (probe P6): after a successful commit a second `commit()` returns silently, a
`write` is accepted into a new stage, and its commit fails `OSError` 39; there is no SEALED
state. In-flight calls are parked by a hook inside their I/O (a batch iterator for a write, the
stage's `publish` for a commit), and the arriving call answers while the first is parked. The
reconcile column is the machine's (`tests/unit/publication/test_sink_state.py`); the sink gains
`reconcile()` with restart recovery.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.publication import PublicationRefused
from decoy_engine.publication._sink_state import SinkState
from tests.unit.execution.publication._sink_support import (
    POLICIES,
    sink_factory,
    stages,
    table,
    tree,
)

S = SinkState


def _refused(call: Any) -> str:
    with pytest.raises(PublicationRefused) as info:
        call()
    assert info.value.receipt.outcome == "refused"
    return info.value.code


class _ParkedWrite:
    """`write_batches` parked inside its batch stream, on another thread."""

    def __init__(self, sink: Any) -> None:
        self.entered, self.release = threading.Event(), threading.Event()
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(sink,), daemon=True)
        self._thread.start()
        assert self.entered.wait(10)  # non-vacuity: the write is in flight

    def _batches(self) -> Iterator[pa.RecordBatch]:
        self.entered.set()
        self.release.wait(10)
        yield from table("A").to_batches()

    def _run(self, sink: Any) -> None:
        try:
            sink.write_batches("customers", self._batches(), schema=table("A").schema)
        except BaseException as exc:  # recorded for the test thread
            self.error = exc

    def finish(self) -> None:
        self.release.set()
        self._thread.join(10)
        assert not self._thread.is_alive()


class _ParkedCommit:
    """`commit()` parked inside the stage's publish, on another thread."""

    def __init__(self, sink: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered, self.release = threading.Event(), threading.Event()
        self.error: BaseException | None = None
        txn_type = type(sink._txn)
        real = txn_type.publish

        def parked(txn: Any) -> None:
            self.entered.set()
            self.release.wait(10)
            real(txn)

        monkeypatch.setattr(txn_type, "publish", parked)
        self._thread = threading.Thread(target=self._run, args=(sink,), daemon=True)
        self._thread.start()
        assert self.entered.wait(10)  # non-vacuity: the commit is in flight

    def _run(self, sink: Any) -> None:
        try:
            sink.commit()
        except BaseException as exc:
            self.error = exc

    def finish(self) -> None:
        self.release.set()
        self._thread.join(10)
        assert not self._thread.is_alive()


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_sealed(policy: str, tmp_path: Path) -> None:
    """SEALED: write refused `publication_attempt_sealed`, commit returns R with no I/O,
    abort is a no-op; nothing new is staged and the bound unit is unchanged."""
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        assert sink.state is S.OPEN
        sink.write("customers", table("A"))
        assert sink.state is S.OPEN
        assert sink.commit() is None
        assert sink.state is S.SEALED
        before = tree(tmp_path / "out")
        assert _refused(lambda: sink.write("orders", table("B"))) == "publication_attempt_sealed"
        assert (
            _refused(lambda: sink.write_batches("o", iter(()), schema=table("B").schema))
            == "publication_attempt_sealed"
        )
        assert stages(tmp_path) == []
        assert sink.commit() is None
        sink.abort()
        assert sink.state is S.SEALED
    assert tree(tmp_path / "out") == before


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_sealed_commit_does_no_io(
    policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.commit()
        assert sink.state is S.SEALED
        out = sink._out
        calls: list[str] = []
        for name in ("begin", "ensure_dir"):
            real = getattr(out, name)

            def spy(*args: Any, _name: str = name, _real: Any = real, **kw: Any) -> Any:
                calls.append(_name)
                return _real(*args, **kw)

            monkeypatch.setattr(out, name, spy)
        assert sink.commit() is None
        assert calls == []
    assert sorted(os.listdir(tmp_path / "out")) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_writing(policy: str, tmp_path: Path) -> None:
    """WRITING: every other call is busy `publication_write_in_progress` and answers while the
    write is parked; an abort is pending and applies on the exit (OPEN becomes ABORTED)."""
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        parked = _ParkedWrite(sink)
        assert sink.state is S.WRITING
        busy = "publication_write_in_progress"
        assert _refused(lambda: sink.write("orders", table("B"))) == busy
        assert _refused(sink.commit) == busy
        sink.abort()  # pending: returns while the write is still parked
        assert sink.state is S.WRITING
        assert len(stages(tmp_path)) == 1
        parked.finish()
        assert parked.error is None
        assert sink.state is S.ABORTED
        assert stages(tmp_path) == []
        assert _refused(lambda: sink.write("orders", table("B"))) == "publication_attempt_aborted"
        assert _refused(sink.commit) == "publication_attempt_aborted"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_writing_ok_returns_to_open(policy: str, tmp_path: Path) -> None:
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        parked = _ParkedWrite(sink)
        parked.finish()
        assert parked.error is None
        assert sink.state is S.OPEN
        sink.write("orders", table("B"))
        sink.commit()
    assert sorted(os.listdir(tmp_path / "out")) == ["customers.parquet", "orders.parquet"]


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_committing(
    policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COMMITTING: busy `publication_commit_in_progress`; a pending abort does not undo a
    commit that seals."""
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.write("customers", table("A"))
        parked = _ParkedCommit(sink, monkeypatch)
        assert sink.state is S.COMMITTING
        busy = "publication_commit_in_progress"
        assert _refused(lambda: sink.write("orders", table("B"))) == busy
        assert _refused(sink.commit) == busy
        sink.abort()
        assert sink.state is S.COMMITTING
        parked.finish()
        assert parked.error is None
        assert sink.state is S.SEALED
    assert pq.read_table(tmp_path / "out" / "customers.parquet").equals(table("A"))
    assert stages(tmp_path) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_committing_failure_with_a_pending_abort(
    policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep.txt").write_text("occupant", encoding="utf-8")
    before = tree(tmp_path / "out")
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.write("customers", table("A"))
        parked = _ParkedCommit(sink, monkeypatch)
        sink.abort()
        parked.finish()
        assert isinstance(parked.error, OSError)
        assert sink.state is S.ABORTED  # FAILED plus the pending abort
        assert stages(tmp_path) == []
    assert tree(tmp_path / "out") == before


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_failed_commit(policy: str, tmp_path: Path) -> None:
    """COMMITTING, an error before the rename: FAILED; every call but abort is refused with the
    code the attempt ended with, and abort removes the stage (ABORTED)."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep.txt").write_text("occupant", encoding="utf-8")
    before = tree(tmp_path / "out")
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.write("customers", table("A"))
        with pytest.raises(OSError):
            sink.commit()
        assert sink.state is S.FAILED
        assert len(stages(tmp_path)) == 1  # the failed rename leaves the stage for abort
        assert _refused(lambda: sink.write("orders", table("B"))) == "publication_failed"
        assert _refused(sink.commit) == "publication_failed"
        sink.abort()
        assert sink.state is S.ABORTED
        assert stages(tmp_path) == []
        sink.abort()
        assert _refused(sink.commit) == "publication_attempt_aborted"
    assert tree(tmp_path / "out") == before


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_failed_write(policy: str, tmp_path: Path) -> None:
    def boom() -> Iterator[pa.RecordBatch]:
        yield from table("A").to_batches()
        raise RuntimeError("stream failed")

    with sink_factory(policy, tmp_path) as make:
        sink = make()
        with pytest.raises(RuntimeError, match="stream failed"):
            sink.write_batches("customers", boom(), schema=table("A").schema)
        assert sink.state is S.FAILED
        assert _refused(sink.commit) == "publication_failed"
        sink.abort()
        assert sink.state is S.ABORTED
        assert stages(tmp_path) == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_open_abort(policy: str, tmp_path: Path) -> None:
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.abort()
        assert sink.state is S.ABORTED
        sink.abort()
        assert _refused(lambda: sink.write("orders", table("B"))) == "publication_attempt_aborted"
        assert _refused(sink.commit) == "publication_attempt_aborted"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_sink_transition_cells_an_invalid_name_is_refused_before_the_state_moves(
    policy: str, tmp_path: Path
) -> None:
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        with pytest.raises(ValueError, match="single path component"):
            sink.write("../x", table("A"))
        assert sink.state is S.OPEN
        sink.write("customers", table("A"))
        sink.commit()
    assert sorted(os.listdir(tmp_path / "out")) == ["customers.parquet"]

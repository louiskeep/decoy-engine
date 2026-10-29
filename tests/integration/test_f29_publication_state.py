"""F29 acceptance A9: a failure after commit preserves the output and says it is partial.

The sidecar (quarantine) is published after the tables commit. If it fails, the tables stay
committed, are never aborted, and the reason is `publication_partial` with no replay advice.
Custom sinks and best-effort aborts report `attempted` or `unknown`, never a confirmed cleanup
the engine did not check.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.reasons import (
    CleanupState,
    Operation,
    Outcome,
    Phase,
    PublicationState,
    RetryClass,
)

from ._f29_jobs import ENGINE_VERSION, fk_job


class _CountingSink(ParquetTransactionalSink):
    def __init__(self, target: Path) -> None:
        super().__init__(target)
        self.aborts = 0

    def abort(self) -> None:
        self.aborts += 1
        super().abort()


class _PlainSink:
    """A custom sink that keeps no publication evidence."""

    def __init__(self, *, fail_commit: bool = False, fail_abort: bool = False) -> None:
        self.fail_commit, self.fail_abort = fail_commit, fail_abort
        self.written: list[str] = []
        self.aborts = 0

    def write(self, table: str, data: pa.Table) -> None:
        self.written.append(table)

    def write_batches(self, table, batches, *, schema) -> None:
        self.written.append(table)

    def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("commit exploded")

    def abort(self) -> None:
        self.aborts += 1
        if self.fail_abort:
            raise RuntimeError("abort exploded")


def _run(tmp_path: Path, sink):
    cfg, sources = fk_job(tmp_path)
    return run_pipeline(
        cfg, sources, engine_version=ENGINE_VERSION, execution_mode="sequential", sink=sink
    )


def test_post_commit_failure_preserves_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sidecar_fails(**kwargs):
        raise OSError(28, "No space left on device", "/secret/quarantine.jsonl")

    monkeypatch.setattr(
        "decoy_engine.execution._sequential.finalize_committed_quarantine", sidecar_fails
    )
    sink = _CountingSink(tmp_path / "out")
    with pytest.raises(OSError) as info:
        _run(tmp_path, sink)
    # The committed tables survive, and the committed output was never aborted.
    assert sorted(p.name for p in (tmp_path / "out").glob("*.parquet")) == [
        "customers.parquet",
        "orders.parquet",
    ]
    assert sink.aborts == 0
    record = reason_from_exception(info.value)
    assert (record.namespace, record.code) == ("decoy", "publication_partial")
    assert record.outcome is Outcome.PUBLICATION and record.retry_class is RetryClass.NEVER
    assert record.phase is Phase.PUBLISH
    assert record.context.publication_state is PublicationState.PARTIAL
    assert record.context.operation is Operation.SIDECAR
    assert "secret" not in repr(record) and "quarantine.jsonl" not in repr(record)


def test_a_custom_sink_without_evidence_is_unknown_and_attempted(tmp_path: Path) -> None:
    sink = _PlainSink(fail_commit=True)
    with pytest.raises(RuntimeError, match="commit exploded") as info:
        _run(tmp_path, sink)
    assert sink.aborts == 1  # the failed commit is aborted, exactly as before
    record = reason_from_exception(info.value)
    assert record.context.operation is Operation.COMMIT
    assert record.context.publication_state is PublicationState.UNKNOWN
    assert (
        record.context.cleanup_state is CleanupState.ATTEMPTED
    )  # we called abort; nothing proves it


def test_a_failing_abort_never_masks_the_primary_failure(tmp_path: Path) -> None:
    sink = _PlainSink(fail_commit=True, fail_abort=True)
    with pytest.raises(RuntimeError, match="commit exploded") as info:  # the primary, not the abort
        _run(tmp_path, sink)
    record = reason_from_exception(info.value)
    assert record.context.cleanup_state is CleanupState.FAILED
    assert record.code != "cleanup_failed"  # cleanup failure is secondary state, not the reason


def test_the_reference_sink_confirms_cleanup_by_looking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(src, dst):
        raise OSError(1, "Operation not permitted", "/secret/dst")

    monkeypatch.setattr("decoy_engine.execution._transactional_sink.os.replace", refuse)
    sink = ParquetTransactionalSink(tmp_path / "out")
    with pytest.raises(OSError) as info:
        _run(tmp_path, sink)
    record = reason_from_exception(info.value)
    assert record.context.publication_state is PublicationState.NOT_PUBLISHED
    assert record.context.cleanup_state is CleanupState.CONFIRMED
    assert not list(tmp_path.glob("_decoy_stage_*"))
    assert reasons.CleanupState.FAILED is not record.context.cleanup_state

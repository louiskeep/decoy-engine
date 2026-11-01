"""F2: the mixed route publishes through ONE transactional session.

Every generate table and every masked table is written into the same
`OutputPublish` session in FK-topological order, with exactly one commit. Any
failure aborts the session (best effort, never raising), publishes nothing, and
surfaces the original error; a commit that never happens is not swallowed.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution import _chunked_output_sink as output_sink
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.native import _chunked_entry
from tests.parity._f2_mixed_support import (
    CHILD,
    PARENT,
    RecordingSink,
    assert_outputs_identical,
    child_table,
    mixed_config,
    oracle,
    run,
)

KEYS = [str(i % 5) for i in range(40)]


def _job(tmp_path: Path) -> tuple[dict, pa.Table]:
    child = child_table(pa.array(KEYS, pa.string()))
    return mixed_config(tmp_path, child), child


def test_success_writes_parent_then_child_into_one_session_and_commits_once(
    tmp_path: Path,
) -> None:
    config, child = _job(tmp_path)
    sink = RecordingSink()
    result = run(config, {CHILD: child}, sink=sink)
    assert sink.calls == [("write", PARENT), ("write_batches", CHILD), ("commit", "")]
    assert sink.committed == 1 and sink.aborted == 0
    assert result.outputs == {}
    assert result.quality_metrics["execution"]["outputs_streamed"] is True
    assert_outputs_identical(sink.published or {}, oracle(config, {CHILD: child}).outputs)


def test_a_generate_write_failure_aborts_before_any_masked_write(tmp_path: Path) -> None:
    config, child = _job(tmp_path)
    sink = RecordingSink(fail_on=f"write:{PARENT}")
    with pytest.raises(RuntimeError, match="injected sink failure on write people"):
        run(config, {CHILD: child}, sink=sink)
    assert sink.aborted == 1 and sink.committed == 0 and sink.published is None
    assert sink.staged == {}
    assert [c for c in sink.calls if c[0] == "write_batches"] == []


def test_a_failure_after_generate_and_a_masked_batch_are_staged_aborts(tmp_path: Path) -> None:
    config, child = _job(tmp_path)
    sink = RecordingSink(fail_on="write_batches_late")
    with pytest.raises(RuntimeError, match="after a staged masked batch"):
        run(config, {CHILD: child}, sink=sink)
    assert ("write", PARENT) in sink.calls and ("write_batches", CHILD) in sink.calls
    assert sink.aborted == 1 and sink.committed == 0
    assert sink.published is None and sink.staged == {}
    assert ("commit", "") not in sink.calls


def test_a_commit_failure_aborts_and_surfaces_the_original_error(tmp_path: Path) -> None:
    config, child = _job(tmp_path)
    sink = RecordingSink(fail_on="commit")
    with pytest.raises(RuntimeError, match="injected sink failure on commit"):
        run(config, {CHILD: child}, sink=sink)
    assert sink.aborted == 1 and sink.committed == 0 and sink.published is None


def test_an_abort_that_raises_does_not_mask_the_original_error(tmp_path: Path) -> None:
    config, child = _job(tmp_path)

    class BrokenAbort(RecordingSink):
        def abort(self) -> None:
            super().abort()
            raise OSError("cleanup failed")

    sink = BrokenAbort(fail_on="commit")
    with pytest.raises(RuntimeError, match="injected sink failure on commit"):
        run(config, {CHILD: child}, sink=sink)
    assert sink.aborted == 1


def test_a_session_that_never_commits_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, child = _job(tmp_path)
    monkeypatch.setattr(output_sink.OutputPublish, "commit", lambda self: None)
    sink = RecordingSink()
    with pytest.raises(ExecutionError) as excinfo:
        run(config, {CHILD: child}, sink=sink)
    assert excinfo.value.code == "internal_publish_not_committed"
    assert sink.aborted == 1 and sink.committed == 0


def test_the_real_sink_publishes_both_tables_atomically(tmp_path: Path) -> None:
    config, child = _job(tmp_path)
    target = tmp_path / "published"
    run(config, {CHILD: child}, sink=ParquetTransactionalSink(target))
    assert sorted(p.name for p in target.iterdir()) == [f"{CHILD}.parquet", f"{PARENT}.parquet"]
    want = oracle(config, {CHILD: child}).outputs
    got = {name: pq.read_table(target / f"{name}.parquet") for name in want}
    assert_outputs_identical(got, want)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("_decoy_stage_")]


def test_the_real_sink_publishes_nothing_when_the_mask_stream_fails_midway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, child = _job(tmp_path)
    real = _chunked_entry.run_mask_chunked

    def failing(*args, **kwargs):
        for index, chunk in enumerate(real(*args, **kwargs)):
            if index == 1:
                raise RuntimeError("mask stream failed on the second chunk")
            yield chunk

    monkeypatch.setattr(_chunked_entry, "run_mask_chunked", failing)
    target = tmp_path / "published"
    with pytest.raises(RuntimeError, match="second chunk"):
        run(config, {CHILD: child}, sink=ParquetTransactionalSink(target))
    assert not target.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("_decoy_stage_")]


def test_the_real_sink_commit_failure_leaves_the_target_untouched(tmp_path: Path) -> None:
    config, child = _job(tmp_path)
    target = tmp_path / "published"
    target.mkdir()
    (target / "keep.txt").write_text("existing")
    with pytest.raises(OSError):
        run(config, {CHILD: child}, sink=ParquetTransactionalSink(target))
    assert [p.name for p in target.iterdir()] == ["keep.txt"]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("_decoy_stage_")]

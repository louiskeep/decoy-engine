"""The quarantine file carries row values, so a run the post-scan gate blocks must not leave one.
Both routes hand the write to `publish_after_gate`, which holds it until the verdict passes. All
values are made up."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._pipeline_finalize import finalize_validators_and_quarantine
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.execution.pipeline._post_scan import (
    PostScanGate,
    publish_after_gate,
    settle,
)
from decoy_engine.quarantine import finalize_committed_quarantine
from decoy_engine.verification.postmask import PostScanBlockedError

REL = {
    "parent": {"table": "p", "columns": ["id"]},
    "children": [{"table": "c", "columns": ["pid"]}],
    "orphan_policy": "preserve",
}
KINDS = {"p": "mask", "c": "mask"}
SOURCE = {"p": pa.table({"id": ["a", "b", "c"]}), "c": pa.table({"pid": ["a", "b", "c"]})}


@pytest.fixture
def gate() -> Any:
    bound = PostScanGate({"relationships": [REL]})
    assert bound.required
    token = _post_scan._ACTIVE.set(bound)
    yield bound
    _post_scan._ACTIVE.reset(token)


def _quarantine_a_child_row(tmp_path: Path) -> Path:
    qpath = tmp_path / "q.jsonl"
    record = RowErrorRecord(table="c", column="pid", row_index=0, trigger="format_error", reason="")
    config = {
        "quarantine": {"enabled": True, "output_path": str(qpath), "triggers": ["format_error"]}
    }
    finalize_validators_and_quarantine(
        {"p": SOURCE["p"], "c": SOURCE["c"]},
        config=config,
        caller_sources={},
        mask_row_errors=(record,),
        quality_metrics={},
    )
    return qpath


def _settle(gate: PostScanGate, outputs: dict[str, pa.Table]) -> None:
    ctx = SimpleNamespace(caller_sources=SOURCE, table_kinds=KINDS)
    settle(gate, ctx, SimpleNamespace(outputs=outputs, quality_metrics={}))  # type: ignore[arg-type]


def test_a_blocked_full_frame_run_leaves_no_quarantine_file(tmp_path: Path, gate: Any) -> None:
    qpath = _quarantine_a_child_row(tmp_path)
    assert not qpath.exists()
    collapsed = {"p": pa.table({"id": ["x", "x", "x"]}), "c": pa.table({"pid": ["b", "c"]})}
    with pytest.raises(PostScanBlockedError):
        gate.run_resident(collapsed, SOURCE, KINDS)
    assert not qpath.exists()


def test_a_passing_run_publishes_the_quarantine_file_when_it_settles(
    tmp_path: Path, gate: Any
) -> None:
    qpath = _quarantine_a_child_row(tmp_path)
    assert not qpath.exists()
    _settle(gate, {"p": SOURCE["p"], "c": pa.table({"pid": ["b", "c"]})})
    assert qpath.exists()


def test_a_sequential_sidecar_waits_for_the_verdict(tmp_path: Path, gate: Any) -> None:
    qpath = tmp_path / "q.jsonl"
    finalize_committed_quarantine(
        sink=None,
        is_genuine_transactional_sink=False,
        written_tables=(),
        output_path=str(qpath),
        entries=[{"table": "c", "row_index": 0}],
        counts_by_trigger={"format_error": 1},
        publish=publish_after_gate,
    )
    assert not qpath.exists()
    _settle(gate, dict(SOURCE))
    assert qpath.exists()


def test_a_gate_that_will_not_judge_the_run_publishes_at_once(tmp_path: Path, gate: Any) -> None:
    gate.deferred = True
    written: list[int] = []
    publish_after_gate(lambda: written.append(1))
    assert written == [1]


def test_outside_a_run_the_write_happens_at_once() -> None:
    written: list[int] = []
    publish_after_gate(lambda: written.append(1))
    assert written == [1]


def _bucketize_run(tmp_path: Path, mode: str, *, broken: bool) -> Path:
    from decoy_engine import run_pipeline
    from tests.unit.execution import _subset_run_support as subset_sup
    from tests.unit.execution.test_sequential_run_kills import _fk_bucketize_config, _sources

    qpath = tmp_path / "q.jsonl"
    config = _fk_bucketize_config(
        tmp_path, {"enabled": True, "output_path": str(qpath), "triggers": ["format_error"]}
    )
    if broken:  # one constant parent key collapses the unique keys the input held
        parent = next(t for t in config["tables"] if t["name"] == "parent")
        parent["columns"][0] = {"name": "id", "strategy": "redact"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(subset_sup.VAR, "ab" * 32)
        run_pipeline(config, sources=_sources(config), engine_version="ps8-q", execution_mode=mode)
    return qpath


@pytest.mark.parametrize("mode", ["full_frame", "sequential"])
def test_a_run_with_a_quarantined_row_publishes_the_file(tmp_path: Path, mode: str) -> None:
    assert _bucketize_run(tmp_path, mode, broken=False).exists()


@pytest.mark.parametrize("mode", ["full_frame", "sequential"])
def test_a_blocked_run_with_a_quarantined_row_writes_no_file(tmp_path: Path, mode: str) -> None:
    with pytest.raises(PostScanBlockedError):
        _bucketize_run(tmp_path, mode, broken=True)
    assert not (tmp_path / "q.jsonl").exists()

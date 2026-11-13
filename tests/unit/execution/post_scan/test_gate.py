"""AT-31: the post-scan gate core (plan 4.5), called directly; no route wires it yet.

`run_resident` stamps `keys` and `verdict` once; `before_commit` refuses a required gate that never
ran; every error the gate raises has an empty chain and carries no value. All values are made up.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.config import post_scan_gate_applies
from decoy_engine.errors import PipelineValidationError
from decoy_engine.execution.out_of_core import resolve_ooc_memory_limit
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.execution.pipeline._post_scan import PostScanGate
from decoy_engine.profiling import MEMORY_LIMIT
from decoy_engine.verification.postmask import PostScanBlockedError, TableBatches, from_table

CANARY = "CANARY-31c4"
REL = {
    "parent": {"table": "p", "columns": ["id"]},
    "children": [{"table": "c", "columns": ["pid"]}],
    "orphan_policy": "preserve",
}
KEYS = {"relationships": [REL]}
BOTH = {"relationships": [REL], "post_scan": {"integrity": True}}
SOURCES = {"p": pa.table({"id": ["a", "b"]}), "c": pa.table({"pid": ["a", "b"]})}
KINDS = {"p": "mask", "c": "mask"}


def run(gate: PostScanGate, child: Sequence[str] = ("a", "b")) -> None:
    outputs = {"p": SOURCES["p"], "c": pa.table({"pid": list(child)})}
    gate.run_resident(outputs, SOURCES, KINDS)


def blocked(call: Any) -> PostScanBlockedError:
    with pytest.raises(PostScanBlockedError) as caught:
        call()
    exc = caught.value
    assert exc.__cause__ is None and exc.__context__ is None
    assert CANARY not in str(exc) and CANARY not in repr(exc.report)
    return exc


def raising(error: BaseException) -> Any:
    def call(*args: Any, **kwargs: Any) -> Any:
        raise error

    return call


@pytest.mark.parametrize(
    ("config", "applies"),
    [
        ({}, False),
        ({"relationships": [], "post_scan": {"integrity": False}}, False),
        (KEYS, True),
        ({"post_scan": {"integrity": True}}, True),
        (BOTH, True),
    ],
)
def test_the_gate_applies_when_a_key_or_integrity_is_declared(
    config: dict[str, Any], applies: bool
) -> None:
    assert post_scan_gate_applies(config) is applies
    assert PostScanGate(config).required is applies


def test_run_resident_stamps_keys_and_verdict() -> None:
    gate = PostScanGate(KEYS)
    run(gate)
    assert gate.ran and gate.verdict is not None and not gate.verdict.blocking
    assert gate.report is not None
    assert sorted((k["table"], k["kind"]) for k in gate.report["keys"]) == [
        ("c", "foreign"),
        ("p", "primary"),
    ]
    assert gate.report["verdict"] == {"blocking": False, "codes": [], "warnings": []}
    assert "residual_pii" not in gate.report


def test_integrity_adds_the_column_suite_over_the_mask_tables() -> None:
    gate = PostScanGate(BOTH)
    run(gate)
    assert gate.report is not None
    assert {"keys", "verdict", "residual_pii", "fk_preservation"} <= set(gate.report)
    assert [f["severity"] for f in gate.report["fk_preservation"]] == ["info"]


def test_a_second_run_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = PostScanGate(KEYS)
    run(gate)
    report = gate.report
    monkeypatch.setattr(_post_scan, "check_keys", raising(AssertionError("ran twice")))
    run(gate, child=("z",))
    assert gate.report is report


def test_a_gate_that_is_not_required_does_nothing() -> None:
    gate = PostScanGate({})
    run(gate, child=(CANARY,))
    gate.before_commit(object())
    assert (gate.ran, gate.report) == (False, None)


def test_before_commit_after_a_run_lets_the_commit_through() -> None:
    gate = PostScanGate(KEYS)
    run(gate)
    gate.before_commit(object())
    gate.before_commit(object())
    assert gate.ran


def test_before_commit_on_a_required_unrun_gate_refuses() -> None:
    gate = PostScanGate(KEYS)
    for _ in range(2):
        exc = blocked(lambda: gate.before_commit(object()))
        assert exc.reason == "post_scan.check_error" and exc.verdict.blocking
    assert not gate.ran


def test_a_blocking_verdict_raises_with_an_empty_chain() -> None:
    gate = PostScanGate(KEYS)
    exc = blocked(lambda: run(gate, child=("a", CANARY)))
    assert exc.reason == "post_scan.foreign_key_orphans_introduced"
    assert gate.ran and gate.report == exc.report


def _failing(table: TableBatches) -> TableBatches:
    def read(columns: Sequence[str], batch_rows: int) -> Iterator[pa.RecordBatch]:
        raise RuntimeError(f"cannot read {CANARY}")

    return replace(table, read=read)


def test_a_key_check_error_result_raises_check_error(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = {"p": SOURCES["p"], "c": pa.table({"pid": ["a", "b"]})}

    def reading(name: str, table: pa.Table) -> TableBatches:
        made = from_table(name, table)
        return _failing(made) if table is outputs["c"] else made

    monkeypatch.setattr(_post_scan, "from_table", reading)
    gate = PostScanGate(KEYS)
    exc = blocked(lambda: gate.run_resident(outputs, SOURCES, KINDS))
    assert exc.reason == "post_scan.check_error"


@pytest.mark.parametrize("part", ["check_keys", "run_post_mask"])
def test_a_part_failing_with_an_uncoded_error_is_stripped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, part: str
) -> None:
    monkeypatch.setattr(_post_scan, part, raising(ValueError(f"bad value {CANARY}")))
    gate = PostScanGate(BOTH)
    with caplog.at_level(logging.DEBUG):
        exc = blocked(lambda: run(gate))
    assert exc.reason == "post_scan.check_error"
    assert exc.report["pass_failed_with"] == "ValueError"
    assert CANARY not in caplog.text
    assert gate.ran


@pytest.mark.parametrize("part", ["check_keys", "run_post_mask"])
def test_a_coded_error_from_a_part_propagates_as_the_same_object(
    monkeypatch: pytest.MonkeyPatch, part: str
) -> None:
    error = PipelineValidationError("x", code=reasons.config.SUBSET_RUN_UNSUPPORTED)
    monkeypatch.setattr(_post_scan, part, raising(error))
    gate = PostScanGate(BOTH)
    with pytest.raises(PipelineValidationError) as caught:
        run(gate)
    assert caught.value is error
    assert not gate.ran


def test_the_memory_limit_is_the_smaller_of_the_scan_cap_and_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small = 256 * 1024 * 1024
    assert PostScanGate(KEYS, budget_bytes=small).memory_limit == (
        resolve_ooc_memory_limit(small).memory_limit
    )
    assert PostScanGate(KEYS, budget_bytes=64 * 1024**3).memory_limit == MEMORY_LIMIT
    seen: list[str] = []
    real = _post_scan.check_keys

    def spy(*args: Any, memory_limit: str, **kwargs: Any) -> Any:
        seen.append(memory_limit)
        return real(*args, memory_limit=memory_limit, **kwargs)

    monkeypatch.setattr(_post_scan, "check_keys", spy)
    run(PostScanGate(KEYS, budget_bytes=small))
    assert seen == [resolve_ooc_memory_limit(small).memory_limit]

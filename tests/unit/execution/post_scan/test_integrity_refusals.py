"""An explicit `post_scan.integrity: true` never runs unchecked: it refuses what it cannot judge.

A sink the gate cannot read, a deferred gate, and a transform-bearing table on a staged commit all
refuse with a registered code before anything is published. Declared keys alone keep deferring.
All values are made up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

import decoy_engine.config as config_api
from decoy_engine import run_pipeline
from decoy_engine.execution import ExecutionError
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.profile import LazySource
from tests._declared_schema import declare_rest
from tests.unit.execution import _subset_run_support as subset_sup
from tests.unit.execution._b6a_support import RecordingSink
from tests.unit.execution._transform_testkit import ENGINE_VERSION, fk_config, fk_tables
from tests.unit.execution.post_scan.test_wiring import _blocked, _config, _run
from tests.unit.execution.test_lazy_path_route_admission import (
    _N,
    _ooc_eligible_config,
    _parent_child_tables,
)

UNREADABLE = "post_scan_integrity_sink_unreadable"
TRANSFORMED = "post_scan_integrity_transforms_unsupported"
FILTER_PARENTS = [{"op": "filter", "expression": "n >= 2"}]


@pytest.fixture(autouse=True)
def _subset_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(subset_sup.VAR, "ab" * 32)


def _refused(call: Any, code: str) -> ExecutionError:
    with pytest.raises(ExecutionError) as caught:
        call()
    exc = caught.value
    assert str(exc.code) == code, exc.code
    assert exc.__cause__ is None and exc.__context__ is None
    return exc


# ----- a sink the gate cannot read ----------------------------------------------------------


@pytest.mark.parametrize("mode", ["auto", "sequential", "out_of_core", "full_frame"])
def test_integrity_with_a_foreign_sink_refuses_before_the_sink_is_touched(
    tmp_path: Path, mode: str
) -> None:
    sink = RecordingSink()
    _refused(lambda: _run(_config(tmp_path, integrity=True), mode, sink=sink), UNREADABLE)
    assert sink.calls == []


def test_a_foreign_sink_with_declared_keys_alone_still_defers(tmp_path: Path) -> None:
    sink = RecordingSink()
    result, _ = _run(_config(tmp_path), "sequential", sink=sink)
    assert ("commit",) in sink.calls
    assert "post_scan_gate_deferred" in result.quality_metrics


def test_integrity_with_the_engine_sink_is_not_refused(tmp_path: Path) -> None:
    result, _ = _run(
        _config(tmp_path, integrity=True),
        "sequential",
        sink=ParquetTransactionalSink(tmp_path / "published"),
    )
    assert "post_scan_gate_deferred" not in result.quality_metrics


def test_the_refusal_predicate_is_one_shared_function() -> None:
    refusal = getattr(config_api, "post_scan_sink_refusal", None)
    assert refusal is not None
    on = {"post_scan": {"integrity": True}}
    foreign = "tests.unit.execution._b6a_support.RecordingSink"
    engine = "decoy_engine.execution._transactional_sink.ParquetTransactionalSink"
    assert str(refusal(on, foreign, deferred=False)) == UNREADABLE
    assert str(refusal(on, engine, deferred=True)) == UNREADABLE
    assert refusal(on, engine, deferred=False) is None
    assert refusal(on, "None", deferred=True) is None
    assert refusal({"relationships": [{"x": 1}]}, foreign, deferred=True) is None


# ----- a transform-bearing table on a staged commit ------------------------------------------


def test_integrity_with_a_transform_bearing_table_refuses_a_staged_commit(tmp_path: Path) -> None:
    parent, child = fk_tables()
    cfg = fk_config(tmp_path, parent, child, parent_transforms=FILTER_PARENTS)
    cfg = declare_rest(cfg, {"parent": parent, "child": child})
    cfg["post_scan"] = {"integrity": True}
    out = tmp_path / "published"
    lazy = {n: LazySource(Path(cfg["sources"][n]["path"])) for n in ("parent", "child")}
    _refused(
        lambda: run_pipeline(
            cfg,
            lazy,
            engine_version=ENGINE_VERSION,
            execution_mode="sequential",
            sink=ParquetTransactionalSink(out),
        ),
        TRANSFORMED,
    )
    assert not out.exists()


# ----- a real finding, no monkeypatch: text_redact told to look only for SSNs leaves emails --------------------------------------------------------


def test_an_email_the_redaction_missed_blocks_a_staged_sequential_run(
    tmp_path: Path,
) -> None:
    parent, child = _parent_child_tables()
    parent = parent.set_column(
        1, "note", pa.array([f"user{i}@example.com" for i in range(_N)], pa.string())
    )
    cfg = _ooc_eligible_config(tmp_path, "parquet", tables=(parent, child))
    cfg["post_scan"] = {"integrity": True}
    table = next(t for t in cfg["tables"] if t["name"] == "parent")
    table["columns"] = [
        (
            {"name": "note", "strategy": "text_redact", "provider_config": {"detectors": ["ssn"]}}
            if c["name"] == "note"
            else c
        )
        for c in table["columns"]
    ]
    out = tmp_path / "published"
    exc = _blocked(
        lambda: _run(
            cfg,
            "sequential",
            sources={"parent": parent, "child": child},
            sink=ParquetTransactionalSink(out),
        )
    )
    assert exc.reason == "post_scan.finding_blocked"
    assert any(f["severity"] == "fail" for f in exc.report["residual_pii"])
    assert not out.exists()

"""AT-16, AT-20, AT-25, AT-14 and AT-24: the post-scan gate wired into `run_pipeline` (plan 4.5).

Every route that returns resident outputs runs the gate before its result is visible; a route that
reads its output back to block is deferred only when its inputs cannot be re-read.
A blocking verdict commits nothing. All values are made up.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons, run_pipeline
from decoy_engine.execution._chunked_input import capture_source_facts
from decoy_engine.execution._publication_observer import observed_commit
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.execution.pipeline._post_scan import PostScanGate
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.verification.postmask import PostScanBlockedError
from tests.unit.execution import _subset_run_support as subset_sup
from tests.unit.execution._b6a_support import RecordingSink
from tests.unit.execution.test_lazy_path_route_admission import (
    _ooc_eligible_config,
    _parent_child_tables,
)

V = "ps8-wiring"
CANARY = "CANARY-7f3a"
DUPLICATES = "post_scan.primary_key_duplicates_introduced"

# A parent key redacted to one constant collapses the unique keys the inputs held.
BREAK_PARENT = {"name": "id", "strategy": "redact"}


@pytest.fixture(autouse=True)
def _subset_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(subset_sup.VAR, "ab" * 32)


def _config(tmp_path: Path, *, broken: bool = False, integrity: bool = False) -> dict[str, Any]:
    config = copy.deepcopy(_ooc_eligible_config(tmp_path, "parquet"))
    if broken:
        parent = next(t for t in config["tables"] if t["name"] == "parent")
        parent["columns"] = [BREAK_PARENT, *(c for c in parent["columns"] if c["name"] != "id")]
    if integrity:
        config["post_scan"] = {"integrity": True}
    return config


class _Routes:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def on_event(self, event: Any) -> None:
        if event.kind == "route":
            self.seen.append(event.route)


def _run(
    config: dict[str, Any], mode: str = "auto", sources: Any = None, **kw: Any
) -> tuple[Any, list[str]]:
    parent, child = _parent_child_tables()
    routes = _Routes()
    result = run_pipeline(
        config,
        sources=sources if sources is not None else {"parent": parent, "child": child},
        engine_version=V,
        execution_mode=mode,
        progress=routes,
        **kw,
    )
    return result, routes.seen


def _blocked(call: Callable[[], Any]) -> PostScanBlockedError:
    with pytest.raises(PostScanBlockedError) as caught:
        call()
    exc = caught.value
    assert exc.__cause__ is None and exc.__context__ is None
    return exc


ROUTES = [
    pytest.param("sequential", id="sequential"),
    pytest.param("out_of_core", id="out_of_core"),
    pytest.param("full_frame", id="full_frame"),
]


@pytest.mark.parametrize("route", ROUTES)
def test_at16_a_blocking_verdict_commits_nothing_on_every_no_sink_route(
    tmp_path: Path, route: str
) -> None:
    seen: list[str] = []
    config = _config(tmp_path, broken=True)

    def run() -> Any:
        result, routes = _run(config, route)
        seen.extend(routes)
        return result

    exc = _blocked(run)
    assert exc.reason == DUPLICATES
    assert not list(tmp_path.glob("*.out.parquet"))
    # The same job, unbroken, runs on this route and hands its outputs back resident. A route that
    # never ran would pass the glob check above for the wrong reason.
    control, routes = _run(_config(tmp_path), route)
    assert routes == [route]
    assert set(control.outputs) == {"parent", "child"}
    assert not list(tmp_path.glob("*.out.parquet"))


def test_at16_a_commit_with_no_output_the_gate_can_read_is_refused() -> None:
    sink = RecordingSink()
    gate = PostScanGate({"relationships": [{"x": 1}]})
    token = _post_scan._ACTIVE.set(gate)
    try:
        exc = _blocked(lambda: observed_commit(sink))
    finally:
        _post_scan._ACTIVE.reset(token)
    assert exc.reason == "post_scan.check_error" and sink.calls == []


def test_at16_a_gate_that_ran_lets_the_commit_through() -> None:
    sink = RecordingSink()
    gate = PostScanGate({"relationships": [{"x": 1}]})
    gate.ran = True
    token = _post_scan._ACTIVE.set(gate)
    try:
        observed_commit(sink)
    finally:
        _post_scan._ACTIVE.reset(token)
    assert sink.calls == [("commit",)]


DEFERRED = {"namespace": "post_scan", "code": "gate_deferred_route"}
FAIL = {"severity": "fail"}


@pytest.mark.parametrize(
    ("mode", "integrity"),
    [("full_frame", False), ("auto", True)],
    ids=["keys-full_frame", "integrity-auto"],
)
def test_at16_a_resident_sink_run_is_gated_before_the_sink_is_touched(
    tmp_path: Path, mode: str, integrity: bool
) -> None:
    sink = RecordingSink()
    config = _config(tmp_path, broken=True, integrity=integrity)
    exc = _blocked(lambda: _run(config, mode, sink=sink))
    assert exc.reason == DUPLICATES
    assert sink.calls == []


@pytest.mark.parametrize("route", ["sequential", "out_of_core"])
def test_at16_a_bounded_route_cannot_take_a_sink_the_gate_cannot_read(
    tmp_path: Path, route: str
) -> None:
    sink = RecordingSink()
    with pytest.raises(Exception, match="post_scan_keys_resident"):
        _run(_config(tmp_path, broken=True), route, sink=sink)
    assert sink.calls == []


def test_at16_integrity_with_a_sink_and_lazy_inputs_still_withholds_on_a_failing_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        finding = {"table": "customers", "column": "email", "detector_id": "email"}
        return {"schema_version": "post-scan-integrity/v2", "residual_pii": [finding | FAIL]}

    monkeypatch.setattr(_post_scan, "run_post_mask", failing)
    cfg, _, lazy = subset_sup.job(tmp_path)
    cfg["subset"] = None
    cfg["post_scan"] = {"integrity": True}
    sink = RecordingSink()
    exc = _blocked(
        lambda: run_pipeline(cfg, lazy, engine_version=V, sink=sink, execution_mode="full_frame")
    )
    assert exc.reason == "post_scan.finding_blocked"
    assert sink.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_at20_the_gate_runs_once_and_the_report_rides_on_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    calls: list[int] = []
    real = _post_scan.check_keys

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(_post_scan, "check_keys", counted)
    result, routes = _run(_config(tmp_path), route)
    assert routes == [route]
    report = result.quality_metrics["post_scan_integrity"]
    assert report["schema_version"] == "post-scan-integrity/v2"
    assert report["verdict"] == {"blocking": False, "codes": [], "warnings": []}
    assert {(k["table"], k["kind"]) for k in report["keys"]} == {
        ("parent", "primary"),
        ("child", "foreign"),
    }
    assert "residual_pii" not in report  # keys alone never run the column suite
    assert len(calls) == 1


def test_at20_a_job_with_no_keys_and_no_integrity_gets_no_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["relationships"] = []
    result, _ = _run(config, "full_frame")
    assert "post_scan_integrity" not in result.quality_metrics


def test_at20_integrity_adds_the_column_suite_to_the_same_report(tmp_path: Path) -> None:
    result, routes = _run(_config(tmp_path, integrity=True))
    assert routes == ["full_frame"]  # the column suite needs the whole output
    report = result.quality_metrics["post_scan_integrity"]
    assert {"keys", "verdict", "residual_pii", "fk_preservation"} <= set(report)


def test_at20_lazy_inputs_with_no_sink_run_the_blocking_gate(tmp_path: Path) -> None:
    cfg, _, lazy = subset_sup.job(tmp_path)
    cfg["subset"] = None
    result, _ = _run(cfg, "sequential", sources=lazy)
    assert "post_scan_gate_deferred" not in result.quality_metrics
    assert result.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False


@pytest.mark.serial
def test_at20_a_gate_re_reads_lazy_inputs_in_batches_and_reports(tmp_path: Path) -> None:
    cfg, data, lazy = subset_sup.job(tmp_path)
    kinds = {name: "mask" for name in lazy}
    gate = PostScanGate(
        {"relationships": cfg["relationships"]}, source_facts=capture_source_facts(lazy, kinds)
    )
    gate.run_resident(data, lazy, kinds)
    assert gate.report is not None and gate.report["verdict"]["blocking"] is False
    assert {k["table"] for k in gate.report["keys"]} >= {"customers", "orders", "items"}


def test_at25_a_gate_that_never_ran_is_refused_after_the_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PostScanGate, "run_resident", lambda *a, **k: None)
    exc = _blocked(lambda: _run(_config(tmp_path), "sequential"))
    assert exc.reason == "post_scan.check_error"


def test_a_job_the_gate_does_not_apply_to_is_untouched_by_the_post_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PostScanGate, "run_resident", lambda *a, **k: None)
    config = _config(tmp_path)
    config["relationships"] = []
    result, _ = _run(config, "full_frame")
    assert "post_scan_integrity" not in result.quality_metrics


def test_at14_no_value_reaches_the_report_the_error_the_reason_or_the_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"cannot read {CANARY}")

    monkeypatch.setattr(_post_scan, "check_keys", boom)
    with caplog.at_level(logging.DEBUG):
        exc = _blocked(lambda: _run(_config(tmp_path), "sequential"))
    record = reason_from_exception(exc)
    assert f"{record.namespace}.{record.code}" == "post_scan.check_error"
    assert exc.report["pass_failed_with"] == "RuntimeError"
    for text in (str(exc), repr(exc.report), repr(record), caplog.text):
        assert CANARY not in text


def test_at14_a_duplicated_canary_key_is_counted_not_quoted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    parent, child = _parent_child_tables()
    marked = parent.set_column(
        0, "id", pa.array([f"{CANARY}-{i % 3}" for i in range(parent.num_rows)])
    )
    with caplog.at_level(logging.DEBUG):
        exc = _blocked(
            lambda: _run(
                _config(tmp_path, broken=True),
                "sequential",
                sources={"parent": marked, "child": child},
            )
        )
    for text in (str(exc), repr(exc.report), caplog.text):
        assert CANARY not in text


def test_the_gate_is_bound_for_the_run_and_unbound_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _post_scan.active_gate() is None
    seen: list[PostScanGate | None] = []
    real = _post_scan.settle

    def spy(gate: PostScanGate, *args: Any) -> None:
        seen.append(_post_scan.active_gate())
        real(gate, *args)

    monkeypatch.setattr(_post_scan, "settle", spy)
    _run(_config(tmp_path), "full_frame")
    assert len(seen) == 1 and seen[0] is not None and seen[0].ran
    assert _post_scan.active_gate() is None


def test_at24_a_subset_run_checks_the_staged_inputs_not_the_whole_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, data, _ = subset_sup.job(tmp_path)
    seen: dict[str, int] = {}
    real = _post_scan.check_keys

    def spy(rels: Any, sources: Any, outputs: Any, **kwargs: Any) -> Any:
        seen.update({name: table.num_rows for name, table in sources.items()})
        return real(rels, sources, outputs, **kwargs)

    monkeypatch.setattr(_post_scan, "check_keys", spy)
    result = run_pipeline(cfg, data, engine_version=V)
    kept = {n: t.num_rows for n, t in result.outputs.items()}
    assert kept["customers"] < data["customers"].num_rows
    assert seen == kept
    assert result.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False


def test_at24_a_subset_run_with_a_broken_key_still_blocks(tmp_path: Path) -> None:
    cfg, data, _ = subset_sup.job(tmp_path)
    customers = next(t for t in cfg["tables"] if t["name"] == "customers")
    customers["columns"] = [
        {"name": "cid", "strategy": "redact"} if c["name"] == "cid" else c
        for c in customers["columns"]
    ]
    exc = _blocked(lambda: run_pipeline(cfg, data, engine_version=V))
    assert exc.reason == DUPLICATES


def test_a_lazy_input_that_changes_before_the_gate_reads_it_is_refused(tmp_path: Path) -> None:
    cfg, data, lazy = subset_sup.job(tmp_path)
    kinds = {name: "mask" for name in lazy}
    gate = PostScanGate(
        {"relationships": cfg["relationships"]}, source_facts=capture_source_facts(lazy, kinds)
    )
    pq.write_table(data["customers"].slice(0, 7), Path(cfg["sources"]["customers"]["path"]))
    with pytest.raises(Exception) as caught:
        gate.run_resident(data, lazy, kinds)
    assert getattr(caught.value, "code", None) == reasons.runtime.LAZY_SOURCE_CHANGED


# ----- a generated parent: orphans the inputs carried are inherited ----------------------------

GENERATED_REL = {
    "parent": {"table": "people", "columns": ["id"]},
    "children": [{"table": "accounts", "columns": ["person_id"]}],
    "orphan_policy": "preserve",
}
GENERATED_KINDS = {"people": "generate", "accounts": "mask"}


def _generated_parent_run(source_child: list[str], output_child: list[str]) -> PostScanGate:
    gate = PostScanGate({"relationships": [GENERATED_REL]})
    gate.run_resident(
        {
            "people": pa.table({"id": ["0", "1", "2"]}),
            "accounts": pa.table({"person_id": output_child}),
        },
        {"accounts": pa.table({"person_id": source_child})},
        GENERATED_KINDS,
    )
    return gate


def test_orphans_the_inputs_carried_to_a_generated_parent_do_not_block() -> None:
    keep = ["0", "1", "zzz", "zzz"]
    gate = _generated_parent_run(keep, keep)
    (child,) = (k for k in gate.report["keys"] if k["kind"] == "foreign")
    assert (child["baseline"], child["source"]["orphan_rows"], child["output"]["orphan_rows"]) == (
        "measured",
        2,
        2,
    )
    assert gate.verdict is not None and not gate.verdict.blocking


def test_a_mask_that_breaks_a_matched_key_of_a_generated_parent_still_blocks() -> None:
    exc = _blocked(
        lambda: _generated_parent_run(["0", "1", "zzz", "zzz"], ["q", "1", "zzz", "zzz"])
    )
    assert exc.reason == "post_scan.foreign_key_orphans_introduced"


# ----- the mixed route and a sink ---------------------------------------------------------------


# ----- key types the join casts between: the gate compares them the way the join does ---------

MASK_REL = {
    "parent": {"table": "parent", "columns": ["id"]},
    "children": [{"table": "child", "columns": ["pid"]}],
    "orphan_policy": "preserve",
}
MASK_KINDS = {"parent": "mask", "child": "mask"}


def _typed_run(
    parent_keys: pa.Array, child_keys: pa.Array, source_child: pa.Array | None = None
) -> PostScanGate:
    gate = PostScanGate({"relationships": [MASK_REL]})
    sources = {
        "parent": pa.table({"id": parent_keys}),
        "child": pa.table({"pid": source_child if source_child is not None else child_keys}),
    }
    gate.run_resident(
        {"parent": pa.table({"id": parent_keys}), "child": pa.table({"pid": child_keys})},
        sources,
        MASK_KINDS,
    )
    return gate


def test_a_string_parent_with_an_int64_child_is_all_orphans_in_both_and_not_blocked() -> None:
    # The join never matches the string "1" to the integer 1, so the orphans came with the data.
    gate = _typed_run(pa.array(["0", "1", "2"]), pa.array([0, 1, 2, 2], type=pa.int64()))
    assert gate.ran and not gate.deferred
    assert gate.verdict is not None and not gate.verdict.blocking
    assert gate.report is not None
    (child,) = (k for k in gate.report["keys"] if k["kind"] == "foreign")
    assert child["status"] == "checked"
    assert (child["source"]["orphan_rows"], child["output"]["orphan_rows"]) == (4, 4)


def test_an_int64_parent_with_a_float_child_still_blocks_a_key_the_mask_broke() -> None:
    exc = _blocked(
        lambda: _typed_run(
            pa.array([0, 1, 2], type=pa.int64()),
            pa.array([0.0, 9.0, 2.0]),
            source_child=pa.array([0, 1, 2], type=pa.int64()),
        )
    )
    assert exc.reason == "post_scan.foreign_key_orphans_introduced"


def test_a_refused_value_in_the_source_too_is_reported_and_does_not_block() -> None:
    # The run did not make these ends incomparable, so the mismatch is inherited, not caused.
    gate = _typed_run(pa.array([0, 1]), pa.array(["unencodable", "1"]))
    assert gate.ran and not gate.deferred
    assert gate.verdict is not None and not gate.verdict.blocking


def test_an_int64_key_past_2_53_against_a_float_parent_is_an_introduced_orphan() -> None:
    # It has no float image, so it equals no float key: the join token keeps it apart.
    exc = _blocked(
        lambda: _typed_run(
            pa.array([1.0]),
            pa.array([2**60 + 1], type=pa.int64()),
            source_child=pa.array([1], type=pa.int64()),
        )
    )
    assert exc.reason == "post_scan.foreign_key_orphans_introduced"


def _mixed_job(tmp_path: Path) -> tuple[dict[str, Any], Any]:
    from tests.parity._f2_mixed_support import child_table, mixed_config

    child = child_table(pa.array([str(i) for i in range(5)] * 8, pa.string()))
    return mixed_config(tmp_path, child), child


def _mixed_run(tmp_path: Path, **kw: Any) -> tuple[Any, list[str]]:
    from tests.parity._f2_mixed_support import CHILD, run

    cfg, child = _mixed_job(tmp_path)
    routes = _Routes()
    return run(cfg, {CHILD: child}, progress=routes, **kw), routes.seen


def test_at20_the_mixed_route_with_no_sink_is_checked_over_its_stitched_outputs(
    tmp_path: Path,
) -> None:
    from tests.parity._f2_mixed_support import COMPANION_PRESENT

    if not COMPANION_PRESENT:
        pytest.skip("decoy-engine-native companion not installed")
    result, routes = _mixed_run(tmp_path)
    assert "mixed" in routes
    report = result.quality_metrics["post_scan_integrity"]
    assert report["verdict"]["blocking"] is False
    assert {k["table"] for k in report["keys"]} == {"people", "accounts"}


def test_at20_a_mixed_job_with_a_sink_the_gate_cannot_read_is_gated_resident(
    tmp_path: Path,
) -> None:
    sink = RecordingSink()
    result, routes = _mixed_run(tmp_path, sink=sink)
    assert "mixed" not in routes
    assert "post_scan_gate_deferred" not in result.quality_metrics
    assert result.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False


def test_at24_an_excluded_edge_to_a_generated_parent_leaves_inherited_orphans_unblocked(
    tmp_path: Path,
) -> None:
    from tests.parity._f2_mixed_support import CHILD, FK, child_table, mixed_config, run

    child = child_table(pa.array([str(i) for i in range(9)] * 4, pa.string()))
    cfg = mixed_config(tmp_path, child, parent_rows=5)
    cfg["subset"] = {
        "seeds": [{"table": CHILD, "mode": "sample", "key_columns": [FK], "count": 20}]
    }
    result = run(cfg, {CHILD: child}, mode="full_frame")
    report = result.quality_metrics["post_scan_integrity"]
    assert report["verdict"]["blocking"] is False
    (fk,) = (k for k in report["keys"] if k["kind"] == "foreign")
    assert fk["output"]["orphan_rows"] > 0
    assert fk["output"]["orphan_rows"] == fk["source"]["orphan_rows"]

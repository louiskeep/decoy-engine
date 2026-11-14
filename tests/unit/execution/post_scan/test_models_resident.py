"""AT-18: only a model post-scan ask keeps a run resident; regex-only integrity streams.

`post_scan.integrity: true` with regex detectors takes the bounded routes and stream mode, and the
gate judges the staged output before it commits. A model detector, or `ner_findings: fail`, keeps
the run resident with `route.post_scan_models_resident`. All values are made up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.config import post_scan_resident_reason
from decoy_engine.execution import ExecutionError, _unified_slice, run_pipeline
from decoy_engine.execution._chunked_output_sink import decide_output_mode
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution._transforms_gate import post_scan_resident_for
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.verification.postmask import PostScanBlockedError
from tests.security.test_f29_remediation import _decide
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _subset_run_support as subset_sup
from tests.unit.execution.post_scan.test_wiring import _blocked, _config, _run
from tests.unit.execution.run_stream import _support as rs
from tests.unit.execution.test_auto_chunk_routing import _single_column_job

REGEX = {"post_scan": {"integrity": True}}
MODEL_ASKS = {
    "ner_detector": {"post_scan": {"integrity": True, "detectors": ["regex", "ner"]}},
    "ner_fail": {
        "post_scan": {"integrity": True, "detectors": ["regex", "ner"], "ner_findings": "fail"}
    },
}
FAIL = {"severity": "fail"}


@pytest.fixture(autouse=True)
def _subset_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(subset_sup.VAR, "ab" * 32)


def _code(reason: Any) -> str | None:
    return None if reason is None else str(reason)


def _failing_suite(table: str, column: str) -> Any:
    def failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        finding = {"table": table, "column": column, "detector_id": "email"}
        return {"schema_version": "post-scan-integrity/v2", "residual_pii": [finding | FAIL]}

    return failing


# ----- the registry: one new code, one retired ------------------------------------------------


def test_the_models_resident_code_is_registered_and_unreleased() -> None:
    spec = reasons.find("route", "post_scan_models_resident")
    assert spec is not None
    assert spec.since == reasons.UNRELEASED


def test_the_integrity_requested_code_is_retired_in_favour_of_it() -> None:
    spec = reasons.get_reason("route", "post_scan_integrity_requested")
    assert spec.deprecated_in == reasons.UNRELEASED
    assert spec.replaced_by == ("route", "post_scan_models_resident")


# ----- the one routing predicate ----------------------------------------------------------------


def test_regex_integrity_no_longer_keeps_a_run_resident() -> None:
    assert post_scan_resident_reason(REGEX) is None
    assert post_scan_resident_for(REGEX) is None


@pytest.mark.parametrize("ask", MODEL_ASKS.values(), ids=MODEL_ASKS)
def test_a_model_ask_keeps_the_run_resident_with_the_new_code(ask: dict[str, Any]) -> None:
    assert _code(post_scan_resident_reason(ask)) == "post_scan_models_resident"
    assert _code(post_scan_resident_for(ask)) == "post_scan_models_resident"


def test_a_model_setting_without_integrity_asks_for_nothing() -> None:
    assert post_scan_resident_reason({"post_scan": {"integrity": False}}) is None


def test_the_route_decision_carries_the_new_code_for_a_model_ask() -> None:
    resident = post_scan_resident_for(MODEL_ASKS["ner_detector"])
    route, reason = _decide(post_scan_resident=resident)
    assert route == "full_frame"
    assert _code(reason) == "post_scan_models_resident"


def test_the_output_mode_streams_for_regex_and_stays_resident_for_a_model_ask() -> None:
    def mode(config: dict[str, Any]) -> str:
        return decide_output_mode(
            stream_chunked_output=True,
            sink=ParquetTransactionalSink(Path("unused")),
            dispatcher_enabled=True,
            config=config,
            fidelity_report=False,
            post_validation=False,
        )[0]

    assert mode(REGEX) == "streamed"
    assert mode(MODEL_ASKS["ner_detector"]) == "resident"


# ----- the bounded routes take regex integrity -------------------------------------------------


@pytest.mark.parametrize("route", ["sequential", "out_of_core"])
def test_regex_integrity_takes_the_bounded_route_with_no_sink(tmp_path: Path, route: str) -> None:
    result, routes = _run(_config(tmp_path, integrity=True), route)
    assert routes == [route]
    report = result.quality_metrics["post_scan_integrity"]
    assert {"keys", "verdict", "residual_pii"} <= set(report)


@pytest.mark.parametrize("route", ["sequential", "out_of_core"])
def test_regex_integrity_takes_the_bounded_route_with_a_staged_sink(
    tmp_path: Path, route: str
) -> None:
    out = tmp_path / "published"
    result, routes = _run(
        _config(tmp_path, integrity=True), route, sink=ParquetTransactionalSink(out)
    )
    assert routes == [route]
    assert sorted(p.name for p in out.iterdir()) == ["child.parquet", "parent.parquet"]
    report = result.quality_metrics["post_scan_integrity"]
    assert {"keys", "verdict", "residual_pii", "fk_preservation"} <= set(report)
    assert "post_scan_gate_deferred" not in result.quality_metrics


@pytest.mark.parametrize("route", ["sequential", "out_of_core"])
def test_a_failing_column_finding_on_a_staged_commit_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    monkeypatch.setattr(
        _post_scan, "run_post_mask_tables", _failing_suite("child", "pid"), raising=False
    )
    out = tmp_path / "published"
    exc = _blocked(
        lambda: _run(_config(tmp_path, integrity=True), route, sink=ParquetTransactionalSink(out))
    )
    assert exc.reason == "post_scan.finding_blocked"
    assert not out.exists()
    assert not list(tmp_path.glob("_decoy_stage_*"))


def test_a_staged_run_is_held_back_when_the_column_suite_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("CANARY-7f3a")

    monkeypatch.setattr(_post_scan, "run_post_mask_tables", boom, raising=False)
    out = tmp_path / "published"
    with pytest.raises(PostScanBlockedError) as caught:
        _run(
            _config(tmp_path, integrity=True), "sequential", sink=ParquetTransactionalSink(out)
        )
    assert caught.value.reason == "post_scan.check_error"
    assert "CANARY-7f3a" not in str(caught.value)
    assert not out.exists()


# ----- stream mode ------------------------------------------------------------------------------


def test_stream_mode_no_longer_refuses_regex_integrity(tmp_path: Path) -> None:
    cfg, lazy = rs.single_table_job(tmp_path, rs.two_column_table(30))
    cfg["post_scan"] = {"integrity": True}
    sink, target = rs.engine_sink(tmp_path)
    result = rs.run_stream(cfg, {support.TABLE: lazy}, sink, chunk_size_rows=7)
    assert result.outputs == {}
    assert (target / "t.parquet").exists()
    report = result.quality_metrics["post_scan_integrity"]
    assert "keys" in report and report["verdict"]["blocking"] is False
    assert "residual_pii" in report


@pytest.mark.parametrize("ask", MODEL_ASKS.values(), ids=MODEL_ASKS)
def test_stream_mode_still_refuses_a_model_ask(tmp_path: Path, ask: dict[str, Any]) -> None:
    cfg, lazy = rs.single_table_job(tmp_path, rs.two_column_table(30))
    cfg["post_scan"] = ask["post_scan"]
    sink, target = rs.engine_sink(tmp_path)
    with pytest.raises(ExecutionError) as caught:
        rs.run_stream(cfg, {support.TABLE: lazy}, sink, chunk_size_rows=7)
    assert caught.value.code == reasons.runtime.STREAMING_UNAVAILABLE
    assert not target.exists()


# ----- the unified slice ------------------------------------------------------------------------


def test_the_unified_slice_takes_regex_integrity_and_runs_the_gate_on_its_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran_before_settle: list[bool] = []
    real = _post_scan.settle

    def settle(gate: Any, ctx: Any, result: Any) -> Any:
        ran_before_settle.append(gate.ran)
        return real(gate, ctx, result)

    monkeypatch.setattr(_post_scan, "settle", settle)
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    cfg, sources = _single_column_job(tmp_path, "hash")
    cfg["post_scan"] = {"integrity": True}
    result = run_pipeline(cfg, sources=sources, engine_version="ps12-unified")
    assert _unified_slice.QUALITY_METRICS_KEY in result.quality_metrics
    assert ran_before_settle == [True]
    assert "keys" in result.quality_metrics["post_scan_integrity"]


def test_a_blocking_finding_stops_a_unified_slice_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_post_scan, "run_post_mask", _failing_suite("accounts", "val"))
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    cfg, sources = _single_column_job(tmp_path, "hash")
    cfg["post_scan"] = {"integrity": True}
    taken: list[bool] = []
    real = _unified_slice.maybe_run_unified_slice

    def watched(**kwargs: Any) -> Any:
        out = real(**kwargs)
        taken.append(out is not None)
        return out

    monkeypatch.setattr(_unified_slice, "maybe_run_unified_slice", watched)
    exc = _blocked(lambda: run_pipeline(cfg, sources=sources, engine_version="ps12-unified"))
    assert exc.reason == "post_scan.finding_blocked"
    assert taken == [True]

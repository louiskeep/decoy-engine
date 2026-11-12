"""SCAN-ML M4 in `run_pipeline`: AT-M4e (route parity of the report) and AT-M4g (the block).

AT-M4e: the full-frame route and `run_pipeline`'s resident auto-chunk lane give the same report,
model findings included (M-M4e: skip the model passes on the chunked route).
AT-M4g: under `ner_findings: fail` an NER `fail` finding, or an NER pass that raises, makes
`run_pipeline` raise `PostScanBlockedError` carrying the report; `warn` returns. The error's text
is the registry summary and its chain is empty (M-M4g, M-M4g2, M-M4g3).

Model proofs use the real spaCy model and the real classifier pack. The last two tests are routing
unit tests with a fake NER detector, so the block predicate is also exercised where spaCy is absent.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.config import PipelineConfig
from tests._ner import needs_ner
from tests.unit.detection_verification import _scanml_m4_support as s

_V = "scanml-m4"
_FINDINGS = "decoy_engine.verification.postmask._model_findings"
ROWS = 400


def _summary() -> str:
    spec = reasons.find("post_scan", "ner_blocked")
    assert spec is not None, "post_scan.ner_blocked is not registered"
    return spec.summary


def _source(every: int = 2) -> pa.Table:
    notes = s.notes_column(every, n=ROWS)
    frame = pd.DataFrame(
        {
            "notes": notes,
            "email": [f"user{i}@example.com" for i in range(ROWS)],
            "code": [f"c{i}" for i in range(ROWS)],
        }
    )
    return pa.Table.from_pandas(frame, preserve_index=False)


def _cfg(tmp_path: Path, table: pa.Table, post_scan: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "t.csv"
    table.to_pandas().to_csv(path, index=False)
    raw = {
        "version": 2,
        "global_settings": {"seed": 11},
        "post_scan": {"integrity": True, **post_scan},
        "sources": {"t": {"type": "file", "format": "csv", "has_header": True, "path": str(path)}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "notes", "strategy": "text_redact"},
                    {"name": "email", "strategy": "passthrough"},
                    {"name": "code", "strategy": "redact"},
                ],
            }
        ],
        "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _run(cfg: dict[str, Any], table: pa.Table, **kwargs: Any) -> Any:
    from decoy_engine.execution import run_pipeline

    return run_pipeline(cfg, sources={"t": table}, engine_version=_V, **kwargs)


def _routes(cfg: dict[str, Any], table: pa.Table) -> tuple[dict[str, Any], dict[str, Any]]:
    full = _run(cfg, table, auto_chunk=False)
    chunked = _run(cfg, table, auto_chunk_threshold_rows=1, chunk_size_rows=64)
    assert "auto_chunk" in chunked.quality_metrics, "the auto-chunk lane did not run"
    assert chunked.quality_metrics["auto_chunk"]["output"]["mode"] == "resident"
    assert full.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    assert chunked.quality_metrics["auto_chunk"]["mode"] != "full_frame"
    assert chunked.outputs["t"].equals(full.outputs["t"])
    reports = []
    for result in (full, chunked):
        report = dict(result.quality_metrics["post_scan_integrity"])
        report.pop("generated_at")
        reports.append(report)
    return reports[0], reports[1]


def test_classifier_report_is_the_same_on_full_frame_and_chunked_routes(tmp_path: Path) -> None:
    table = _source()
    cfg = _cfg(tmp_path, table, {"detectors": ["regex", "classifier"]})
    full, chunked = _routes(cfg, table)
    assert s.rows(full, "classifier"), full["residual_pii"]
    assert full == chunked


@needs_ner
def test_ner_report_is_the_same_on_full_frame_and_chunked_routes(tmp_path: Path) -> None:
    table = _source()
    cfg = _cfg(tmp_path, table, {"detectors": ["regex", "classifier", "ner"]})
    full, chunked = _routes(cfg, table)
    assert s.rows(full, "ner", column="notes", severity="warning"), full["residual_pii"]
    assert full == chunked


def _assert_safe(err: BaseException) -> None:
    assert str(err) == _summary()
    assert getattr(err, "message", None) == _summary()
    assert err.__cause__ is None and err.__context__ is None
    assert getattr(err, "reason", None) == "post_scan.ner_blocked"
    clone = pickle.loads(pickle.dumps(err))  # noqa: S301 - our own bytes
    assert type(clone) is type(err) and str(clone) == str(err)
    assert clone.report == err.report


@needs_ner
def test_an_ner_fail_finding_blocks_the_run(tmp_path: Path) -> None:
    table = _source()
    cfg = _cfg(tmp_path, table, {"detectors": ["regex", "ner"], "ner_findings": "fail"})
    with pytest.raises(s.blocked_error()) as caught:
        _run(cfg, table)
    err = caught.value
    (finding,) = s.rows(err.report, "ner", column="notes", detector_id="person_name")
    assert (finding["severity"], finding["promoted"]) == ("fail", True)
    assert err.report["schema_version"] == "post-scan-integrity/v2"
    _assert_safe(err)


@needs_ner
def test_warn_mode_returns_with_the_finding_in_the_report(tmp_path: Path) -> None:
    table = _source()
    cfg = _cfg(tmp_path, table, {"detectors": ["regex", "ner"]})
    result = _run(cfg, table)
    report = result.quality_metrics["post_scan_integrity"]
    (finding,) = s.rows(report, "ner", column="notes", detector_id="person_name")
    assert finding["severity"] == "warning"


@needs_ner
def test_fail_mode_with_no_ner_finding_over_the_rate_returns(tmp_path: Path) -> None:
    table = _source(every=10)
    cfg = _cfg(tmp_path, table, {"detectors": ["regex", "ner"], "ner_findings": "fail"})
    report = _run(cfg, table).quality_metrics["post_scan_integrity"]
    (finding,) = s.rows(report, "ner", column="notes", detector_id="person_name")
    assert finding["severity"] == "info"


@needs_ner
def test_a_raising_ner_pass_blocks_and_keeps_the_regex_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    table = _source()
    warn = _cfg(tmp_path, table, {"detectors": ["regex", "ner"]})
    regex = s.rows(
        _run(warn, table, auto_chunk=False).quality_metrics["post_scan_integrity"], "regex"
    )
    assert regex, "the fixture must give a regex finding"
    mf = importlib.import_module(_FINDINGS)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Ask Debra Archer to confirm")

    monkeypatch.setattr(mf, "ner_rates", boom)
    warn_report = _run(warn, table, auto_chunk=False).quality_metrics["post_scan_integrity"]
    assert len(s.rows(warn_report, "ner", severity="error")) == 1  # warn mode: report, no block
    cfg = _cfg(tmp_path, table, {"detectors": ["regex", "ner"], "ner_findings": "fail"})
    with pytest.raises(s.blocked_error()) as caught:
        _run(cfg, table, auto_chunk=False)
    err = caught.value
    assert s.rows(err.report, "regex") == regex
    (error,) = s.rows(err.report, "ner", severity="error")
    assert error["model"]["id"] == "en_core_web_sm"
    assert "Debra" not in repr(err.report)
    _assert_safe(err)


# -- routing unit tests (a fake NER detector; the proofs above use the real model) -------------


def _fake_ner(monkeypatch: pytest.MonkeyPatch, hit_cells: int | None) -> None:
    import importlib

    from decoy_engine.detection._ner_rate import NerRate
    from decoy_engine.detection.types import ModelRef
    from decoy_engine.profiling.model_pack import DetectorSet

    mf = importlib.import_module(_FINDINGS)
    ref = ModelRef("en_core_web_sm", "3.8.0")
    monkeypatch.setattr(
        mf, "load_detectors", lambda sources, **_kw: DetectorSet(tuple(sources), None, ref)
    )
    monkeypatch.setattr(mf, "pipeline_version", lambda _model: ref.version)

    def rates(values: Any, *, model: str) -> dict[str, NerRate]:
        if hit_cells is None:
            raise RuntimeError("fake pass failure")
        return {
            "location": NerRate("location", 0, ROWS),
            "person_name": NerRate("person_name", hit_cells, ROWS),
        }

    monkeypatch.setattr(mf, "ner_rates", rates)


@pytest.mark.parametrize("hit_cells", [300, None], ids=["fail_finding", "pass_error"])
def test_block_predicate_routing_with_a_fake_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hit_cells: int | None
) -> None:
    import importlib.util

    assert importlib.util.find_spec(_FINDINGS) is not None, "the model findings pass is missing"
    _fake_ner(monkeypatch, hit_cells)
    table = _source()
    cfg = _cfg(tmp_path, table, {"detectors": ["regex", "ner"], "ner_findings": "fail"})
    with pytest.raises(s.blocked_error()) as caught:
        _run(cfg, table, auto_chunk=False)
    _assert_safe(caught.value)
    warn = _cfg(tmp_path, table, {"detectors": ["regex", "ner"]})
    assert "post_scan_integrity" in _run(warn, table, auto_chunk=False).quality_metrics


def test_a_regex_fail_alone_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    from decoy_engine.execution import _pipeline_finalize
    from decoy_engine.execution._adapter import ExecutionResult

    compute = _pipeline_finalize.compute_post_scan_integrity
    assert "detectors" in inspect.signature(compute).parameters, "no detectors are passed in"
    _fake_ner(monkeypatch, 0)
    import importlib

    mf = importlib.import_module(_FINDINGS)
    table = pa.table({"email": [f"user{i}@example.com" for i in range(ROWS)]})
    cfg = s.config(
        [s.table_config("t", [])], post_scan={"detectors": ["regex", "ner"], "ner_findings": "fail"}
    )
    result = ExecutionResult(outputs={"t": table}, table_kinds={"t": "mask"})
    detectors = mf.load_detectors(("regex", "ner"), ner_model="en_core_web_sm")
    compute(result, config=cfg, sources={"t": table}, detectors=detectors)
    report = result.quality_metrics["post_scan_integrity"]
    assert s.rows(report, "regex", column="email", severity="fail"), report["residual_pii"]
    assert not s.rows(report, "ner")

"""SCAN-ML M4 on the one blocking decision: model rows block only through the NER predicate.

Owner rulings (plan 1 rows on `ner_findings: fail` and a raising model, 4.5 severity, order and
enforcement, AT-M4f, AT-M4g, Q1): a `residual_pii` row whose `source` is `ner` or `classifier`
skips the generic severity rule. Under `ner_findings: fail` an NER row at `fail` or `error` adds
`ner_blocked`; under `warn` no model row blocks; a classifier row never blocks. A model row with a
severity outside the known set still adds `check_error`. `ner_blocked` ranks last, so a report
that also has a blocking regex row keeps that row's code first. A model pass lets a coded engine
error through unchanged (`passes_through`) and turns any other exception into one `error` row
stamped with `source` and `model`.

Mutations: m1 routes model rows through the generic rule (C and D red), m2 drops the classifier
exclusion (D red), m3 swaps `passes_through` for a bare `except Exception` (the coded case red).
The plain-exception case is the AT-M4g row shape, tied to M-M4g2.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import reasons
from tests._ner import needs_ner
from tests.unit.detection_verification import _scanml_m4_support as s

_BLOCKED = "decoy_engine.verification.postmask._blocked"
_FINDINGS = "decoy_engine.verification.postmask._model_findings"
_NER = ("en_core_web_sm", "3.8.0")
ROWS = 400


def _verdict(report: dict[str, Any], mode: str) -> Any:
    from decoy_engine.verification.postmask import post_scan_verdict

    params = inspect.signature(post_scan_verdict).parameters
    assert "ner_findings" in params, "the verdict does not read the ner_findings mode"
    return post_scan_verdict(report, ner_findings=mode)


def _codes(report: dict[str, Any], mode: str) -> list[str]:
    return [c.code for c in _verdict(report, mode).codes]


def _row(severity: Any, source: str | None, table: str = "t", column: str = "notes") -> Any:
    row: dict[str, Any] = {
        "table": table,
        "column": column,
        "detector_id": "person_name" if source == "ner" else "email",
        "severity": severity,
        "message": "value-free",
    }
    if source is not None:
        row["source"] = source
    if source in ("ner", "classifier"):
        row["model"] = {"id": _NER[0], "version": _NER[1]}
    return row


def _report(*rows: Any) -> dict[str, Any]:
    return {
        "schema_version": "post-scan-integrity/v2",
        "residual_pii": list(rows),
        "fk_preservation": [],
        "policy_validation": [],
        "pass_failed_with": None,
    }


# ----- cases A to E: the NER predicate -------------------------------------------------------


@pytest.mark.parametrize("severity", ["fail", "error"], ids=["A_ner_fail", "B_ner_error"])
def test_an_ner_fail_or_error_row_under_fail_blocks_with_ner_blocked(severity: str) -> None:
    verdict = _verdict(_report(_row(severity, "ner")), "fail")
    assert verdict.blocking is True
    assert [c.code for c in verdict.codes] == ["ner_blocked"]


@pytest.mark.parametrize("severity", ["error", "fail"], ids=["C_ner_error", "C_ner_fail"])
def test_under_warn_no_ner_row_blocks(severity: str) -> None:
    verdict = _verdict(_report(_row(severity, "ner")), "warn")
    assert (verdict.blocking, verdict.codes) == (False, ())


@pytest.mark.parametrize("mode", ["warn", "fail"])
@pytest.mark.parametrize("severity", ["error", "fail"])
def test_d_a_classifier_row_never_blocks(severity: str, mode: str) -> None:
    verdict = _verdict(_report(_row(severity, "classifier")), mode)
    assert (verdict.blocking, verdict.codes) == (False, ())


@pytest.mark.parametrize("mode", ["warn", "fail"])
def test_e_an_ner_warning_never_blocks(mode: str) -> None:
    verdict = _verdict(_report(_row("warning", "ner"), _row("info", "ner")), mode)
    assert (verdict.blocking, verdict.codes) == (False, ())


def test_the_mode_defaults_to_warn() -> None:
    from decoy_engine.verification.postmask import post_scan_verdict

    _verdict(_report(), "warn")
    assert post_scan_verdict(_report(_row("fail", "ner"))).codes == ()


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="ner_findings"):
        _verdict(_report(), "maybe")


# ----- order and the regex rule beside it ----------------------------------------------------


def test_a_regex_fail_beside_an_ner_fail_keeps_its_code_first() -> None:
    from decoy_engine.verification.postmask import PostScanBlockedError

    report = _report(_row("fail", "regex", column="email"), _row("fail", "ner"))
    verdict = _verdict(report, "fail")
    assert [c.code for c in verdict.codes] == ["finding_blocked", "ner_blocked"]
    error = PostScanBlockedError(report, verdict)
    assert (error.code, error.reason) == ("finding_blocked", "post_scan.finding_blocked")


@pytest.mark.parametrize("source", ["regex", None], ids=["regex", "no_source_key"])
@pytest.mark.parametrize("mode", ["warn", "fail"])
def test_regex_rows_keep_the_generic_rule(source: str | None, mode: str) -> None:
    assert _codes(_report(_row("fail", source)), mode) == ["finding_blocked"]
    assert _codes(_report(_row("error", source)), mode) == ["check_error"]
    assert _codes(_report(_row("warning", source)), mode) == []


@pytest.mark.parametrize("mode", ["warn", "fail"])
@pytest.mark.parametrize("source", ["ner", "classifier"])
@pytest.mark.parametrize("severity", ["bogus", None, 3])
def test_a_malformed_model_row_is_a_check_error(source: str, severity: Any, mode: str) -> None:
    assert _codes(_report(_row(severity, source)), mode) == ["check_error"]


# ----- the blocked error and the adapter -----------------------------------------------------


def test_an_ner_only_block_is_coded_ner_blocked_with_the_registry_summary() -> None:
    from decoy_engine.reason_adapter import reason_from_exception
    from decoy_engine.verification.postmask import PostScanBlockedError

    report = _report(_row("fail", "ner"))
    error = PostScanBlockedError(report, _verdict(report, "fail"))
    summary = reasons.get("post_scan", "ner_blocked").summary
    assert (error.code, str(error), error.message) == ("ner_blocked", summary, summary)
    record = reason_from_exception(error)
    assert (record.namespace, record.code) == ("post_scan", "ner_blocked")


# ----- pass-through in the model passes ------------------------------------------------------


def _fake_set(*sources: str) -> Any:
    from decoy_engine.detection.types import ModelRef
    from decoy_engine.profiling.model_pack import DetectorSet

    pack = {"manifest": SimpleNamespace(pack_id="lgbm-v1", version="1")}
    return DetectorSet(
        ("regex", *sources), pack if "classifier" in sources else None, ModelRef(*_NER)
    )


def _frames() -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    frame = pd.DataFrame(
        {"email": [f"user{i}@example.com" for i in range(ROWS)], "notes": s.notes_column(2)}
    )
    return {"t": frame}, {"t": frame.copy()}


def _break(monkeypatch: pytest.MonkeyPatch, source: str, exc: BaseException) -> None:
    mf = importlib.import_module(_FINDINGS)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(mf, "pipeline_version", lambda _model: _NER[1])
    monkeypatch.setattr(mf, "ner_rates" if source == "ner" else "classify_frame", boom)


@pytest.mark.parametrize("source", ["ner", "classifier"])
def test_a_coded_engine_error_in_a_model_pass_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    from decoy_engine.detection._model_status import ModelUnavailableError
    from decoy_engine.profiling import passes_through

    coded = ModelUnavailableError(reasons.provider.NER_MODEL_NOT_INSTALLED)
    assert passes_through(coded)
    _break(monkeypatch, source, coded)
    sources, outputs = _frames()
    caught: BaseException | None = None
    try:
        s.run_post_mask(sources, outputs, s.config([s.table_config("t", [])]), _fake_set(source))
    except ModelUnavailableError as exc:
        caught = exc
    assert caught is coded, "a coded engine error inside a model pass did not reach the caller"


@pytest.mark.parametrize("source", ["ner", "classifier"])
def test_a_plain_exception_in_a_model_pass_is_one_error_row(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    _break(monkeypatch, source, RuntimeError("Ask Debra Archer to confirm"))
    sources, outputs = _frames()
    cfg = s.config([s.table_config("t", [])])
    report = s.run_post_mask(sources, outputs, cfg, _fake_set(source))
    assert s.rows(report, "regex"), "the regex rows must stay"
    (row,) = s.rows(report, source)
    assert row["severity"] == "error" and "RuntimeError" in row["message"]
    expected = (
        {"id": _NER[0], "version": _NER[1]}
        if source == "ner"
        else {
            "id": "lgbm-v1",
            "version": "1",
        }
    )
    assert row["model"] == expected
    assert "Debra" not in repr(report)


# ----- in the run --------------------------------------------------------------------------


def _main_class() -> type:
    return importlib.import_module(_BLOCKED).PostScanBlockedError  # type: ignore[no-any-return]


def _run_cfg(tmp_path: Path, table: pa.Table, post_scan: dict[str, Any]) -> dict[str, Any]:
    from decoy_engine.config import PipelineConfig

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
                ],
            }
        ],
        "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
    }
    return PipelineConfig.model_validate(raw).model_dump()


@needs_ner
def test_a_run_blocked_only_by_ner_raises_mains_error_coded_ner_blocked(tmp_path: Path) -> None:
    """Case A end to end on the real model: the one blocked-run error, AT-M4g's text and chain."""
    from decoy_engine.execution import run_pipeline
    from decoy_engine.reason_adapter import reason_from_exception

    frame = pd.DataFrame({"notes": s.notes_column(2, n=ROWS), "email": ["x"] * ROWS})
    table = pa.Table.from_pandas(frame, preserve_index=False)
    cfg = _run_cfg(tmp_path, table, {"detectors": ["regex", "ner"], "ner_findings": "fail"})
    caught: BaseException | None = None
    try:
        run_pipeline(cfg, sources={"t": table}, engine_version="scanml-m4")
    except Exception as exc:
        caught = exc
    assert type(caught) is _main_class(), f"not the one blocked-run error: {type(caught)}"
    summary = reasons.get("post_scan", "ner_blocked").summary
    assert (caught.code, caught.reason) == ("ner_blocked", "post_scan.ner_blocked")
    assert [c.code for c in caught.verdict.codes] == ["ner_blocked"]
    assert (str(caught), caught.message) == (summary, summary)
    assert caught.__cause__ is None and caught.__context__ is None
    record = reason_from_exception(caught)
    assert (record.namespace, record.code) == ("post_scan", "ner_blocked")


def test_a_run_with_a_regex_fail_and_an_ner_fail_leads_with_finding_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.detection._ner_rate import NerRate
    from decoy_engine.execution import _pipeline_finalize
    from decoy_engine.execution._adapter import ExecutionResult

    mf = importlib.import_module(_FINDINGS)
    monkeypatch.setattr(mf, "pipeline_version", lambda _model: _NER[1])
    monkeypatch.setattr(
        mf,
        "ner_rates",
        lambda values, *, model: {"person_name": NerRate("person_name", 300, ROWS)},
    )
    frame = pd.DataFrame({"email": [f"user{i}@example.com" for i in range(ROWS)]})
    table = pa.Table.from_pandas(frame, preserve_index=False)
    cfg = s.config(
        [s.table_config("t", [])], post_scan={"detectors": ["regex", "ner"], "ner_findings": "fail"}
    )
    result = ExecutionResult(outputs={"t": table}, table_kinds={"t": "mask"})
    caught: BaseException | None = None
    try:
        _pipeline_finalize.compute_post_scan_integrity(
            result, config=cfg, sources={"t": table}, detectors=_fake_set("ner")
        )
    except Exception as exc:
        caught = exc
    assert type(caught) is _main_class(), f"not the one blocked-run error: {type(caught)}"
    assert [c.code for c in caught.verdict.codes] == ["finding_blocked", "ner_blocked"]
    assert caught.code == "finding_blocked"
    assert caught.__cause__ is None and caught.__context__ is None

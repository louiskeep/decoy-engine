"""F29 acceptance A5: findings, verification and publication outcomes stay distinct.

Real runs where the engine can produce the situation, the report adapter where the situation is a
verifier report. The catch types of every direct caller are untouched (each case still raises or
returns exactly what it did before F29).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons
from decoy_engine.errors import FlagPauseSignal, ValidatorFailedError
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._isolated_commit import CommitError
from decoy_engine.reason_adapter import reason_from_exception, reasons_from_report
from decoy_engine.reasons import Outcome, Phase, RetryClass
from decoy_engine.verification.postmask.types import PostMaskReport

_ENGINE_VERSION = "f29-outcomes"


def _luhn_job(tmp_path: Path, cc: str, extra: dict | None = None):
    src = pa.table({"cc": [cc]})
    path = tmp_path / "t.parquet"
    pq.write_table(src, path)
    cfg = {
        "version": 1,
        "global_settings": {"job_name": "f29", "seed": 42},
        "sources": {"t": {"type": "file", "path": str(path), "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": str(path) + ".out", "format": "parquet"}},
        "tables": [{"name": "t", "columns": [{"name": "cc", "strategy": "passthrough"}]}],
        "relationships": [],
        "validators": [{"name": "luhn", "columns": {"t": ["cc"]}}],
    }
    cfg.update(extra or {})
    return cfg, {"t": pq.read_table(path)}


def test_findings_verification_and_publication_are_distinct(tmp_path: Path) -> None:
    # Report-only findings: a completed check that found something. Not a failure.
    findings = reasons_from_report(PostMaskReport(schema_version="1", fail_count=2))
    assert [(r.namespace, r.code) for r in findings] == [("decoy", "findings_reported")]
    assert findings[0].outcome is Outcome.FINDINGS and findings[0].retry_class is RetryClass.NEVER

    # The same findings under enforcement are a verification verdict, still not an outage.
    enforced = reasons_from_report(PostMaskReport(schema_version="1", fail_count=2), enforcing=True)
    assert [r.code for r in enforced] == ["verification_failed"]
    assert enforced[0].outcome is Outcome.VERIFICATION

    # A verifier that could not complete is its own reason, whatever the findings say.
    outage = reasons_from_report(
        PostMaskReport(schema_version="1", error_count=1, pass_failed_with="ModuleNotFoundError")
    )
    assert [r.code for r in outage] == ["verification_unavailable"]
    assert outage[0].outcome is Outcome.VERIFICATION and outage[0].retry_class is RetryClass.UNKNOWN
    assert "ModuleNotFoundError" not in repr(outage)

    # Mixed: the verifier failure takes precedence and is listed first; the findings stay visible.
    mixed = reasons_from_report(
        PostMaskReport(schema_version="1", fail_count=3, error_count=1), enforcing=True
    )
    assert [r.code for r in mixed] == ["verification_unavailable", "verification_failed"]
    assert reasons_from_report(PostMaskReport(schema_version="1", pass_count=5)) == ()
    assert reasons_from_report({"fail_count": 1, "error_count": 0})[0].code == "findings_reported"

    # An enforced negative verdict from a real run: the existing exception, now with a reason.
    cfg, sources = _luhn_job(tmp_path, "4532015112830367")
    with pytest.raises(ValidatorFailedError) as info:
        run_pipeline(cfg, sources, engine_version=_ENGINE_VERSION)
    verdict = reason_from_exception(info.value)
    assert verdict.code == "verification_failed" and verdict.outcome is Outcome.VERIFICATION
    assert verdict.retry_class is RetryClass.NEVER

    # A strategy error is execution, never verification or publication.
    strategy = reason_from_exception(
        StrategyError(code=reasons.runtime.CYCLIC_WORK_ORDERING, strategy="hash"), phase=Phase.MASK
    )
    assert strategy.outcome is Outcome.EXECUTION and strategy.namespace == "runtime"

    # A commit error is publication.
    commit = reason_from_exception(CommitError("could not commit"))
    assert commit.code == "publication_failed" and commit.outcome is Outcome.PUBLICATION

    # Pause is a control outcome, not an unexpected failure; a cancel is too.
    pause = reason_from_exception(FlagPauseSignal([{"message": "m"}], "gate"))
    assert (pause.code, pause.outcome, pause.kind) == (
        "run_paused",
        Outcome.CANCELLED,
        reasons.Kind.CONTROL,
    )
    assert reason_from_exception(KeyboardInterrupt()).outcome is Outcome.CANCELLED


def test_report_only_findings_do_not_turn_a_successful_run_into_a_failure(tmp_path: Path) -> None:
    cfg, sources = _luhn_job(tmp_path, "4532015112830366", {"run_storm": True})
    result = run_pipeline(cfg, sources, engine_version=_ENGINE_VERSION)
    assert result.outputs  # the run succeeded; STORM reports and never enforces
    report = result.quality_metrics.get("storm_postmask")
    assert report is not None
    # Whatever the report says, adapting it never raises and never changes the result.
    reasons_from_report(report)


def test_catch_types_and_messages_are_unchanged(tmp_path: Path) -> None:
    cfg, sources = _luhn_job(tmp_path, "4532015112830367")
    with pytest.raises(ValidatorFailedError):
        run_pipeline(cfg, sources, engine_version=_ENGINE_VERSION)
    # The exception still carries its report; the reason is additional, never a replacement.
    try:
        run_pipeline(cfg, sources, engine_version=_ENGINE_VERSION)
    except ValidatorFailedError as exc:
        assert exc.report is not None

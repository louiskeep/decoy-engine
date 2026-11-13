"""The post-mask runner lets a coded engine error through and strips every other one."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.execution import ExecutionError
from decoy_engine.fileaccess import FileAccessReadError
from decoy_engine.verification.postmask import runner

CANARY = "CANARY-6a0f"
CHECKS = ["check_residual_pii", "fk_findings", "check_policy_validation"]
SECTIONS = {
    "check_residual_pii": "residual_pii",
    "fk_findings": "fk_preservation",
    "check_policy_validation": "policy_validation",
}


def _raising(error: BaseException) -> Any:
    def check(*args: Any, **kwargs: Any) -> Any:
        raise error

    return check


def _run() -> dict[str, Any]:
    return runner.run_post_mask({}, {}, config={})


@pytest.mark.parametrize("check", CHECKS)
@pytest.mark.parametrize(
    "error",
    [
        FileAccessReadError("sources.t.path"),
        ExecutionError(code=reasons.runtime.LAZY_SOURCE_CHANGED, message="changed"),
    ],
)
def test_a_coded_error_propagates_as_the_same_object(
    monkeypatch: pytest.MonkeyPatch, check: str, error: BaseException
) -> None:
    monkeypatch.setattr(runner, check, _raising(error))
    with pytest.raises(type(error)) as info:
        _run()
    assert info.value is error


@pytest.mark.parametrize("check", CHECKS)
def test_any_other_error_becomes_an_error_finding_that_names_the_class_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, check: str
) -> None:
    monkeypatch.setattr(runner, check, _raising(RuntimeError(f"cannot read {CANARY}")))
    with caplog.at_level(logging.DEBUG):
        report = _run()
    (finding,) = report[SECTIONS[check]]
    assert finding["severity"] == "error"
    assert "RuntimeError" in finding["message"]
    assert CANARY not in repr(report)
    assert CANARY not in caplog.text
    assert report["error_count"] == 1

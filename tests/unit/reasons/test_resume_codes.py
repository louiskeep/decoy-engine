"""AT-P6: the thirteen resume reason codes and their pinned classification."""

from __future__ import annotations

import pytest

from decoy_engine.reasons import Kind, Outcome, RetryClass, Severity, get

_CFG = Outcome.CONFIGURATION
_PUB = Outcome.PUBLICATION
_REFUSALS = (
    "resume_run_dir_invalid",
    "resume_layout_unsupported",
    "resume_route_unsupported",
    "resume_vault_unsupported",
    "resume_platform_unsupported",
    "resume_no_checkpoint",
    "resume_store_invalid",
    "resume_superseded",
    "resume_fingerprint_changed",
    "resume_source_changed",
)

CASES = [
    *((code, Kind.FAILURE, Severity.ERROR, _CFG, RetryClass.NEVER) for code in _REFUSALS),
    ("resume_in_progress", Kind.FAILURE, Severity.ERROR, _CFG, RetryClass.RETRY_OPERATION),
    ("resume_checkpoint_invalid", Kind.DECISION, Severity.WARNING, _PUB, RetryClass.NEVER),
    ("resume_store_not_removed", Kind.WARNING, Severity.WARNING, _PUB, RetryClass.NEVER),
]


def test_thirteen_codes() -> None:
    assert len(CASES) == 13


@pytest.mark.parametrize(("code", "kind", "severity", "outcome", "retry"), CASES)
def test_resume_code_is_pinned(
    code: str, kind: Kind, severity: Severity, outcome: Outcome, retry: RetryClass
) -> None:
    spec = get("decoy", code)
    assert spec.kind is kind
    assert {r.severity for r in spec.rules} == {severity}
    assert {r.outcome for r in spec.rules} == {outcome}
    assert {r.retry for r in spec.rules} == {retry}
    assert spec.since == "unreleased"

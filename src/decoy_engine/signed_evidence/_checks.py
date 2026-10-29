"""Required-check evaluation against the independently trusted catalog.

Pattern: policy evaluation separated from authentication (the in-toto layout / Sigstore policy
split): a signature says who asserted the outcomes; this module decides whether the outcomes
satisfy what the operator requires. The expected subject set is compiled from the trusted catalog
and the caller's expected artifacts, never from the results the producer happened to include, so
absent, duplicated or unknown results fail closed. A no-match PASS counts only with adequate,
machine-readable coverage; an empty or sampled scan is INCONCLUSIVE, never PASS.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._catalog import RequiredCatalog, RequiredCheck
from ._context import ExpectedArtifact
from ._result import CheckResult, CheckStatus
from ._statement import CheckOutcome, EvidenceStatementV1


@dataclass(frozen=True, slots=True)
class CheckEvaluation:
    status: CheckStatus
    results: tuple[CheckResult, ...]
    reasons: tuple[str, ...]
    notes: tuple[str, ...]


def _subjects(
    req: RequiredCheck, expected: tuple[ExpectedArtifact, ...], snapshot_id: str
) -> list[tuple[str, str]]:
    if req.scope == "snapshot":
        return [("snapshot", snapshot_id)]
    return [
        ("artifact", a.id)
        for a in sorted(expected, key=lambda x: x.id)
        if req.media_types is None or a.media_type in req.media_types
    ]


def _judge(req: RequiredCheck, outcome: CheckOutcome) -> tuple[str, str | None]:
    """Return ``(PASS | FAIL | INCONCLUSIVE, reason)`` for one outcome against its requirement."""
    if outcome.status == "FAIL":
        return "FAIL", "CHECK_FAILED"
    if outcome.status == "INCONCLUSIVE":
        return "INCONCLUSIVE", "CHECK_INCONCLUSIVE"
    if outcome.status == "NOT_APPLICABLE":
        if outcome.reason_code in req.not_applicable_reason_codes:
            return "PASS", None
        return "INCONCLUSIVE", "NOT_APPLICABLE_UNJUSTIFIED"
    if outcome.severity == "error":
        return "FAIL", "CHECK_FAILED"
    if outcome.severity == "warning" and not req.allow_warning:
        return "FAIL", "WARNING_NOT_PERMITTED"
    cov = outcome.coverage
    if cov.rows_scanned < req.min_rows_scanned:
        return "INCONCLUSIVE", "COVERAGE_INSUFFICIENT"
    if not req.allow_empty_subject and (cov.rows_total < 1 or cov.rows_scanned < 1):
        return "INCONCLUSIVE", "COVERAGE_INSUFFICIENT"
    if req.require_full_scan and (outcome.limits.sampled or cov.rows_scanned != cov.rows_total):
        return "INCONCLUSIVE", "COVERAGE_INSUFFICIENT"
    return "PASS", None


def evaluate_checks(
    *,
    statement: EvidenceStatementV1,
    catalog: RequiredCatalog,
    expected_artifacts: tuple[ExpectedArtifact, ...],
    expected_snapshot_id: str | None,
) -> CheckEvaluation:
    reasons: set[str] = set()
    notes: list[str] = []
    results: list[CheckResult] = []
    snapshot_id = expected_snapshot_id or statement.source.snapshot_id

    if statement.policy.digest != catalog.digest:
        reasons.add("POLICY_DIGEST_MISMATCH")
    elif statement.policy.id != catalog.id or statement.policy.version != catalog.version:
        reasons.add("POLICY_VERSION_MISMATCH")

    by_key: dict[tuple[str, str, str], list[CheckOutcome]] = {}
    for c in statement.checks:
        by_key.setdefault((c.check_id, c.subject_kind, c.subject_id), []).append(c)

    expected_keys: set[tuple[str, str, str]] = set()
    worst = {"FAIL": False, "INCONCLUSIVE": False}
    for req in catalog.requirements:
        for kind, sid in _subjects(req, expected_artifacts, snapshot_id):
            key = (req.check_id, kind, sid)
            expected_keys.add(key)
            found = by_key.get(key, [])
            label = f"{req.check_id}:{kind}:{sid}"
            if len(found) == 1:
                verdict, why = _judge(req, found[0])
            elif not found:
                verdict, why = "INCONCLUSIVE", "CHECK_SUBJECT_MISSING"
            else:
                verdict, why = "INCONCLUSIVE", "CHECK_SUBJECT_DUPLICATE"
            results.append(CheckResult(req.check_id, kind, sid, not req.optional, verdict, why))
            if req.optional:
                if verdict != "PASS":
                    notes.append(f"OPTIONAL_CHECK_{verdict}:{label}")
                continue
            if verdict == "FAIL":
                worst["FAIL"] = True
            elif verdict == "INCONCLUSIVE":
                worst["INCONCLUSIVE"] = True
            if why:
                reasons.add(why)

    known_ids = {r.check_id for r in catalog.requirements}
    for (cid, kind, sid), _outcomes in sorted(by_key.items()):
        if (cid, kind, sid) in expected_keys:
            continue
        reason = "CHECK_NOT_IN_CATALOG" if cid not in known_ids else "CHECK_UNKNOWN_SUBJECT"
        results.append(CheckResult(cid, kind, sid, False, "INCONCLUSIVE", reason))
        reasons.add(reason)
        worst["INCONCLUSIVE"] = True

    if reasons & {"POLICY_DIGEST_MISMATCH", "POLICY_VERSION_MISMATCH"}:
        worst["INCONCLUSIVE"] = True
    if worst["FAIL"]:
        status = CheckStatus.FAIL
    elif worst["INCONCLUSIVE"]:
        status = CheckStatus.INCONCLUSIVE
    else:
        status = CheckStatus.PASS
    return CheckEvaluation(status, tuple(results), tuple(sorted(reasons)), tuple(sorted(notes)))

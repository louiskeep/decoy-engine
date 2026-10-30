"""Sentry: the retention-report allow-list changes only with a reviewed edit (F26 A2/A23).

The frozen key sets below are the reviewed public surface. Adding a field to a report dataclass
without updating both the allow-list in the module and this snapshot fails the build, so a new
field cannot reach JSON, audit rows or logs unreviewed. The enum snapshots do the same for the
closed reason/exit vocabularies that the platform and CLI both import.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from decoy_engine import retention as r

ROOT = Path(r.__file__).parent

REVIEWED_REPORT = {
    "schema", "kind", "run_id", "policy_id", "scope", "complete", "limits", "counts",
    "reason_counts", "unregistered_count", "outcome", "refusal", "digest", "items",
}  # fmt: skip
REVIEWED_ITEM = {
    "artifact_id", "artifact_class", "decision", "reason", "size_bytes", "result", "error_type",
}  # fmt: skip


def test_reviewed_report_keys_match_the_module_allow_list():
    assert r.ALLOWED_REPORT_KEYS == REVIEWED_REPORT
    assert r.ALLOWED_ITEM_KEYS == REVIEWED_ITEM


def test_dataclass_fields_cannot_grow_without_review():
    item_fields = {f.name for f in dataclasses.fields(r.ReportItem)}
    assert item_fields == REVIEWED_ITEM
    limit_fields = {f.name for f in dataclasses.fields(r.Limits)}
    count_fields = {f.name for f in dataclasses.fields(r.Counts)}
    assert limit_fields == r.ALLOWED_LIMIT_KEYS
    assert count_fields == r.ALLOWED_COUNT_KEYS
    top = {f.name for f in dataclasses.fields(r.RetentionReport)}
    assert top == {
        "kind", "run_id", "policy_id", "scope", "complete", "limits", "counts", "digest",
        "items", "unregistered_count", "refusal",
    }  # fmt: skip


def test_reason_vocabulary_is_append_only():
    # Removing or renaming a wire value breaks every stored report and consumer.
    pinned = {
        "eligible", "retain_hold", "retain_backend_hold", "retain_locator_unreadable",
        "retain_state_changed", "retain_evidence_incomplete", "retain_producer_required",
        "result_deleted", "result_retryable", "refused_gate_off", "refused_digest_mismatch",
        "refused_recovery_unproven", "refused_authorization",
    }  # fmt: skip
    assert pinned <= {c.value for c in r.ReasonCode}


def test_contract_package_is_pure():
    banned = ("boto3", "google.cloud", "sqlalchemy", "fastapi", "os.unlink", "shutil", "open(")
    for py in ROOT.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{py.name} contains {token}"


def test_no_module_over_the_size_cap():
    for py in ROOT.glob("*.py"):
        assert len(py.read_text(encoding="utf-8").splitlines()) <= 600, py.name

"""AT-E2a-8: the two CONFIG-EXPLICIT E2a reasons are registered, owned, pinned and scanned."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.release.reason_manifest_policy import append_only_violations

from decoy_engine import reasons
from decoy_engine.validation_result import CODES

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sentry"))
import _reason_scan as scan

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "f29"
    / "reason-manifest-v1.19-config-explicit-e2a.json"
)

OWNED = {
    "SOURCE_FILE_UNDECLARED_COLUMN": "PipelineValidationError",
    "SOURCE_FILE_UNDECLARED_TABLE": "PipelineValidationError",
}


def test_config_explicit_e2_reasons_registered() -> None:
    for name, owner in OWNED.items():
        code = getattr(reasons.config, name)
        spec = reasons.find("config", code)
        assert spec is not None, name
        assert spec.owner == owner, name
        assert spec.since == "1.19.0", name
        assert getattr(CODES, name) == code, name


def test_config_explicit_e2_reasons_equal_the_frozen_fixture() -> None:
    pinned = json.loads(FIXTURE.read_text())
    codes = {getattr(reasons.config, name) for name in OWNED}
    assert {e["code"] for e in pinned["entries"]} == codes
    live = json.loads(json.dumps(reasons.manifest()))
    live["entries"] = [e for e in live["entries"] if (e["namespace"], e["code"]) in _keys(pinned)]
    assert append_only_violations(pinned, live) == []
    assert pinned["entries"] == live["entries"]


def test_the_declaration_refusals_are_scanned_and_registry_backed() -> None:
    helpers = frozenset({("profile/_csv_reader.py", "source_refusal")})
    sites = [
        s
        for s in scan.scan(scan.SRC, helpers)
        if s.file == "profile/_declared_columns.py" and s.family == "source_refusal"
    ]
    assert len(sites) == 3
    assert {s.value_class for s in sites} == {"registry"}


def _keys(manifest: dict) -> set[tuple[str, str]]:
    return {(e["namespace"], e["code"]) for e in manifest["entries"]}


E2B_FIXTURE = FIXTURE.with_name("reason-manifest-v1.26-config-explicit-e2b-r.json")


def test_declared_column_missing_is_registered_pinned_and_scanned() -> None:
    """AT-E2b-1 registry half: one new code, pinned, raised through the one adapter."""
    code = reasons.config.SOURCE_FILE_DECLARED_COLUMN_MISSING
    spec = reasons.find("config", code)
    assert code == "source_file.declared_column_missing"
    assert spec is not None
    assert (spec.owner, spec.since) == ("PipelineValidationError", "1.26.0")
    assert code == CODES.SOURCE_FILE_DECLARED_COLUMN_MISSING
    pinned = json.loads(E2B_FIXTURE.read_text())
    assert [e["code"] for e in pinned["entries"]] == [code]
    live = json.loads(json.dumps(reasons.manifest()))
    live["entries"] = [e for e in live["entries"] if (e["namespace"], e["code"]) in _keys(pinned)]
    assert append_only_violations(pinned, live) == []
    assert pinned["entries"] == live["entries"]

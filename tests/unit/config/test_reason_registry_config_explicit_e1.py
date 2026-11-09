"""AT-E1-7: the three CONFIG-EXPLICIT E1 reasons are registered, owned and pinned."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.release.reason_manifest_policy import append_only_violations

from decoy_engine import reasons
from decoy_engine.validation_result import CODES

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "f29"
    / "reason-manifest-v1.15-config-explicit-e1.json"
)

OWNED = {
    "SOURCE_FILE_HAS_HEADER_REQUIRED": "decoy_engine.validation_result",
    "SOURCE_FILE_COLUMN_NAMES_SOURCE_CONFLICT": "decoy_engine.validation_result",
    "SOURCE_FILE_COLUMN_NAMES_FILE_INVALID": "PipelineValidationError",
}


def test_config_explicit_e1_reasons_registered() -> None:
    for name, owner in OWNED.items():
        code = getattr(reasons.config, name)
        spec = reasons.find("config", code)
        assert spec is not None, name
        assert spec.owner == owner, name
        assert getattr(CODES, name) == code, name


def test_config_explicit_e1_reasons_equal_the_frozen_fixture() -> None:
    pinned = json.loads(FIXTURE.read_text())
    codes = {getattr(reasons.config, name) for name in OWNED}
    assert {e["code"] for e in pinned["entries"]} == codes
    # The fixture pins only this slice's identities, so compare it with the same slice of the
    # live catalog; the whole-catalog check is test_reason_registry's.
    live = json.loads(json.dumps(reasons.manifest()))
    live["entries"] = [e for e in live["entries"] if (e["namespace"], e["code"]) in _keys(pinned)]
    assert append_only_violations(pinned, live) == []
    assert pinned["entries"] == live["entries"]


def _keys(manifest: dict) -> set[tuple[str, str]]:
    return {(e["namespace"], e["code"]) for e in manifest["entries"]}

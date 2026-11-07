"""A4: the 12 canary reasons are registered under `verification`, in order, at one registry version."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from packaging.version import Version
from scripts.release.reason_manifest_policy import since_window_violations

from decoy_engine import reasons
from decoy_engine.reasons.verification import _canary
from decoy_engine.verification.canary import CanaryReason

PIN = Path(__file__).resolve().parents[3] / "fixtures" / "f29" / "reason-registry-release.json"
RELEASED_SINCE = "1.3.0"
PLAN_ORDER = (
    "canary_match",
    "canary_scan_incomplete",
    "canary_input_rejected",
    "canary_binding_absent",
    "canary_publication_inactive",
    "canary_target_unavailable",
    "canary_publication_mismatch",
    "canary_artifact_unreadable",
    "canary_artifact_unsupported",
    "canary_resource_limit",
    "canary_cell_uncompared",
    "canary_span_short_excluded",
)


def _since_violations(specs, pinned: str, live: str) -> list[str]:
    return since_window_violations(
        ((f"{s.namespace}.{s.code}", s.since) for s in specs), pinned, live
    )


def _canary_specs():
    return [reasons.get("verification", code) for code in PLAN_ORDER]


def test_twelve_reasons_registered_under_verification_in_order() -> None:
    assert tuple(_canary.CANARY_SPAN_REASONS) == PLAN_ORDER
    assert tuple(r.value for r in CanaryReason) == PLAN_ORDER
    owned = [s for s in reasons.specs() if s.owner == "CanarySpan"]
    assert sorted(s.code for s in owned) == sorted(PLAN_ORDER)
    for code, spec in zip(PLAN_ORDER, _canary_specs(), strict=True):
        assert spec.namespace == "verification" and spec.owner == "CanarySpan"
        assert spec.symbol == code.upper() and getattr(_canary, spec.symbol) == code
        assert spec.deprecated_in is None and spec.replaced_by is None
    assert reasons.check_integrity() == []


def test_since_is_frozen_at_the_release_that_shipped_them() -> None:
    # Released with registry 1.3.0 (pinned as a literal, like test_platform_reasons.py pins V):
    # all 12 shipped together, and the release pin counts exactly those 12 at that version.
    pin = json.loads(PIN.read_text())
    assert Version(pin["registry_version"]) >= Version(RELEASED_SINCE)
    specs = _canary_specs()
    assert {s.since for s in specs} == {RELEASED_SINCE}
    assert pin["since_counts"][RELEASED_SINCE] == len(specs) == 12


def test_since_violations_flags_a_released_or_future_version() -> None:
    # Fixed versions, so the check stays meaningful whatever the live registry and pin are.
    pinned, live = "1.4.0", "1.6.0"
    specs = [dataclasses.replace(s, since="1.5.0") for s in _canary_specs()]
    assert _since_violations(specs, pinned, live) == []
    seeded = [dataclasses.replace(specs[0], since=pinned), *specs[1:]]
    assert _since_violations(seeded, pinned, live) == ["verification.canary_match: since 1.4.0"]
    seeded = [dataclasses.replace(specs[0], since="1.7.0"), *specs[1:]]
    assert _since_violations(seeded, pinned, live) == ["verification.canary_match: since 1.7.0"]

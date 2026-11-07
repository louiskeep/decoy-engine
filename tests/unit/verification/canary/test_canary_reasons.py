"""A4: the 12 canary reasons are registered under `verification`, in order, at one registry version."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from packaging.version import Version

from decoy_engine import reasons
from decoy_engine.reasons.verification import _canary
from decoy_engine.verification.canary import CanaryReason

PIN = Path(__file__).resolve().parents[3] / "fixtures" / "f29" / "reason-registry-release.json"
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
    """Specs that claim a released `since` or a version above the live registry's.

    Several unreleased versions can be live in the chain (each slice takes its own minor), so any
    `since` in (pinned, live] is valid.
    """
    return [
        f"{s.namespace}.{s.code}: since {s.since}"
        for s in specs
        if Version(s.since) <= Version(pinned) or Version(s.since) > Version(live)
    ]


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


def test_since_is_unreleased_or_frozen_by_the_release_pin() -> None:
    pin = json.loads(PIN.read_text())
    pinned, live = pin["registry_version"], reasons.REGISTRY_VERSION
    assert Version(live) >= Version(pinned)
    specs = _canary_specs()
    if any(Version(s.since) <= Version(pinned) for s in specs):
        # A release has shipped them: they shipped together, and the release pin counts all 12 at
        # that version, so test_reason_release_pin.py freezes them from here on.
        assert {s.since for s in specs} == {specs[0].since}
        assert pin["since_counts"].get(specs[0].since, 0) >= len(specs)
    else:
        assert _since_violations(specs, pinned, live) == []


def test_since_violations_flags_a_released_or_future_version() -> None:
    # Fixed versions, so the check stays meaningful whatever the live registry and pin are.
    pinned, live = "1.4.0", "1.6.0"
    specs = [dataclasses.replace(s, since="1.5.0") for s in _canary_specs()]
    assert _since_violations(specs, pinned, live) == []
    seeded = [dataclasses.replace(specs[0], since=pinned), *specs[1:]]
    assert _since_violations(seeded, pinned, live) == ["verification.canary_match: since 1.4.0"]
    seeded = [dataclasses.replace(specs[0], since="1.7.0"), *specs[1:]]
    assert _since_violations(seeded, pinned, live) == ["verification.canary_match: since 1.7.0"]

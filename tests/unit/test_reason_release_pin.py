"""The reason catalog an engine release shipped is frozen at its registry version.

The A1 gate (`test_reason_registry.py`) checks the live catalog against the v1 pin
(`reason-manifest-v1.json`, registry 1.0.0) and demands a minor bump only for identities new
relative to THAT pin. Once the live registry has moved past the pin (1.1.0), an identity added at
1.1.0 with no bump passes A1. This pin closes that gap per release: it records the registry version
the last engine release shipped plus a digest of that catalog, so any change at the same version
fails, and per-`since` counts, so a new identity cannot claim an already-released version.

It is a digest and counts, not a second copy of the catalog: the catalog carries the retired
tombstone spellings that only the v1 pin is excepted to hold. `scripts/release/cut-release.sh`
refuses to tag unless this pin describes the live registry (re-capture with
`python scripts/release/reason_release_pin.py --write`).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from packaging.version import Version
from scripts.release.reason_manifest_policy import since_floor_violations
from scripts.release.reason_release_pin import PIN_PATH, pin_violations, release_pin

from decoy_engine import reasons

V1_PIN = Path(__file__).resolve().parents[1] / "fixtures" / "f29" / "reason-manifest-v1.json"


def _pin() -> dict:
    return json.loads(PIN_PATH.read_text())


def _live() -> dict:
    return json.loads(json.dumps(reasons.manifest()))


def test_live_registry_honours_the_released_pin() -> None:
    assert pin_violations(_pin(), _live()) == []


def test_pin_is_well_formed() -> None:
    pin = _pin()
    assert set(pin) == {"registry_version", "manifest_sha256", "identities", "since_counts"}
    assert sum(pin["since_counts"].values()) == pin["identities"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda m: m["entries"].append({**m["entries"][0], "code": "brand_new_code"}),
            "changed without a registry version bump",
        ),
        (
            lambda m: m["entries"][0].__setitem__("summary", "Edited."),
            "changed without a registry version bump",
        ),
        (
            lambda m: (
                m.__setitem__("registry_version", "9.9.0"),
                m["entries"].append({**m["entries"][0], "code": "brand_new_code"}),
            ),
            "claim an already-released since",
        ),
        (lambda m: m.__setitem__("registry_version", "0.0.1"), "went backwards"),
        (
            # A whole released bucket moves to a new version under a bump: no live bucket is
            # left at 1.3.0, so only a check over the pinned buckets sees it drop to 0.
            lambda m: (
                m.__setitem__("registry_version", "9.9.0"),
                [e.__setitem__("since", "9.9.0") for e in m["entries"] if e["since"] == "1.3.0"],
            ),
            "0 identities claim an already-released since 1.3.0",
        ),
    ],
)
def test_pin_mutants_are_rejected(mutate, expected) -> None:
    pin = release_pin(_live())
    live = _live()
    mutate(live)
    assert any(expected in p for p in pin_violations(pin, live))


def test_a_bumped_addition_with_a_new_since_is_accepted() -> None:
    pin = release_pin(_live())
    live = copy.deepcopy(_live())
    live["registry_version"] = "9.9.0"
    live["entries"].append({**live["entries"][0], "code": "brand_new_code", "since": "9.9.0"})
    assert pin_violations(pin, live) == []


def test_an_unbumped_addition_to_the_live_catalog_fails_the_checked_in_pin() -> None:
    # The premise is stated, not inherited: the live catalog sits at the PINNED version (no
    # bump), whatever version the registry has moved to since the release.
    pin = _pin()
    live = copy.deepcopy(_live())
    live["registry_version"] = pin["registry_version"]
    live["entries"].append(
        {**live["entries"][0], "code": "brand_new_code", "since": pin["registry_version"]}
    )
    expected = (
        f"the catalog released as {pin['registry_version']} changed without a registry version bump"
    )
    assert expected in pin_violations(pin, live)


def test_a_bumped_addition_to_the_live_catalog_passes_the_checked_in_pin() -> None:
    # The sibling: the same addition under a version above the pin, claiming that version, is a
    # correct bump and reports nothing.
    pin = _pin()
    pinned = Version(pin["registry_version"])
    bumped = max(
        Version(_live()["registry_version"]), Version(f"{pinned.major}.{pinned.minor + 1}.0")
    )
    live = copy.deepcopy(_live())
    live["registry_version"] = str(bumped)
    live["entries"].append({**live["entries"][0], "code": "brand_new_code", "since": str(bumped)})
    assert bumped > pinned
    assert pin_violations(pin, live) == []


since_violations = since_floor_violations


def test_identities_added_after_the_v1_pin_name_their_own_since() -> None:
    assert since_violations(json.loads(V1_PIN.read_text()), _live()) == []


def test_reasons_doc_states_the_live_registry_version() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "reasons.md").read_text(encoding="utf-8")
    assert f"`registry_version` (`{reasons.REGISTRY_VERSION}`)" in doc

"""Every reason-registry minor above the floor has exactly one complete pinned fixture.

Two slices sharing a minor give two fixtures for it, or one fixture that misses the other's
codes. Limit: this proves one complete fixture per minor, not which slice wrote it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from scripts.release.reason_manifest_policy import (
    FIXTURE_GLOB,
    minor_fixture_violations,
    summary_version_violations,
)

from decoy_engine import reasons

ROOT = Path(__file__).resolve().parents[2]
F29 = ROOT / "tests" / "fixtures" / "f29"
# A fixed historical fact, not the moving version: 1.5.0 is the last minor that shipped
# without a fixture (F31 #377). Never raise it to make room for a new minor.
FLOOR = "1.5.0"


def _live() -> dict:
    return json.loads(json.dumps(reasons.manifest()))


def _fixtures() -> dict[str, dict]:
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in F29.glob(FIXTURE_GLOB)}


def _entry(code: str, since: str) -> dict:
    return {"namespace": "ns", "code": code, "since": since, "summary": "S"}


def _synthetic() -> tuple[dict[str, dict], dict]:
    """Floor 7.1.0, live 7.3.0: one identity at each of 7.2.0 and 7.3.0, one fixture each."""
    a, b, old = _entry("a", "7.2.0"), _entry("b", "7.3.0"), _entry("old", "7.0.0")
    live = {"registry_version": "7.3.0", "entries": [old, a, b]}
    fixtures = {
        "reason-manifest-v7.2-a.json": {"registry_version": "7.2.0", "entries": [a]},
        "reason-manifest-v7.3-b.json": {"registry_version": "7.3.0", "entries": [b]},
    }
    return fixtures, live


def test_live_registry_has_one_complete_fixture_per_minor() -> None:
    assert minor_fixture_violations(_fixtures(), _live(), FLOOR) == []


def test_no_live_summary_names_a_version() -> None:
    assert summary_version_violations(_live()["entries"]) == []


def test_synthetic_baseline_is_clean() -> None:
    fixtures, live = _synthetic()
    assert minor_fixture_violations(fixtures, live, "7.1.0") == []


def test_rejects_two_fixtures_for_one_minor() -> None:
    fixtures, live = _synthetic()
    fixtures["reason-manifest-v7.3-other.json"] = copy.deepcopy(
        fixtures["reason-manifest-v7.3-b.json"]
    )
    assert "minor 7.3 has 2 fixtures (want exactly 1)" in minor_fixture_violations(
        fixtures, live, "7.1.0"
    )


def test_rejects_a_fixture_missing_a_live_identity() -> None:
    fixtures, live = _synthetic()
    live["entries"].append(_entry("c", "7.3.0"))
    assert any(
        "live identity ns.c missing" in p for p in minor_fixture_violations(fixtures, live, "7.1.0")
    )


def test_rejects_a_fixture_with_an_extra_identity() -> None:
    fixtures, live = _synthetic()
    fixtures["reason-manifest-v7.3-b.json"]["entries"].append(_entry("z", "7.3.0"))
    assert any("ns.z is not live" in p for p in minor_fixture_violations(fixtures, live, "7.1.0"))


def test_rejects_a_new_identity_at_the_live_version_without_a_bump() -> None:
    # SUB-STOP M8 shape: an addition claims the current version and the fixture is not updated.
    fixtures, live = _synthetic()
    live["entries"].append(_entry("sneak", "7.3.0"))
    assert minor_fixture_violations(fixtures, live, "7.1.0") != []


def test_rejects_a_live_identity_above_the_floor_with_no_fixture() -> None:
    fixtures, live = _synthetic()
    del fixtures["reason-manifest-v7.2-a.json"]
    assert "minor 7.2 has 0 fixtures (want exactly 1)" in minor_fixture_violations(
        fixtures, live, "7.1.0"
    )
    fixtures, live = _synthetic()
    live["entries"].append(_entry("odd", "7.9.4"))
    assert any(
        "has no fixture slot" in p for p in minor_fixture_violations(fixtures, live, "7.1.0")
    )


def test_rejects_a_highest_fixture_minor_below_the_registry() -> None:
    fixtures, live = _synthetic()
    live["registry_version"] = "7.4.0"
    problems = minor_fixture_violations(fixtures, live, "7.1.0")
    assert "minor 7.4 has 0 fixtures (want exactly 1)" in problems
    assert "highest fixture minor 7.3 is not the registry minor" in problems


# REG-RELEASE E2: a pending (`since` unreleased) identity is named by an exact `namespace/code`
# code span in a changelog fragment; the claim rule and its extraction live in the policy module.
from scripts.release import reason_manifest_policy as pol  # noqa: E402
from scripts.release.stamp_release import is_fragment_name, parse_fragment  # noqa: E402


def _fragment_texts(root: Path) -> list[str]:
    folder = root / "changelog.d"
    return [
        p.read_text(encoding="utf-8")
        for p in sorted(folder.glob("*.md"))
        if is_fragment_name(p.name)
    ]


def _claim_problems(live: dict, texts: list[str]) -> list[str]:
    return pol.pending_fragment_violations(live, [parse_fragment(t).block for t in texts])


def test_every_live_pending_identity_is_named_by_a_fragment() -> None:
    assert _claim_problems(_live(), _fragment_texts(ROOT)) == []


def _pending(*codes: str) -> dict:
    entries = [_entry("old", "7.0.0")]
    entries += [
        {"namespace": "config", "code": c, "since": "unreleased", "summary": "S"} for c in codes
    ]
    return {"registry_version": "7.3.0", "entries": entries}


def _frag(body: str) -> str:
    return f"### Added (probe, SLICE, 2026-11-10)\n\n{body}\n"


def test_at5_an_exact_code_span_names_a_pending_identity() -> None:
    live = _pending("code")
    assert _claim_problems(live, [_frag("Adds `config/code`.")]) == []
    assert (
        _claim_problems(
            live, [_frag("Nothing."), _frag("Also `config/code`, again `config/code`.")]
        )
        == []
    )
    assert _claim_problems(_pending(), []) == [], "numeric identities need no fragment"


@pytest.mark.parametrize(
    "body",
    ["Adds config/code in prose.", "Adds `config.code`.", "Adds `Config/code`.", "Adds `config/Code`.",
     "Adds `config/ code`.", "Adds ` config/code`.", "Adds `config/code/x`.", "Adds `ns/config/code`.",
     "Adds `config/code_extra`.", "Adds `config/cod`.", "Adds config/`code`."],
)  # fmt: skip
def test_at5_no_other_spelling_names_a_pending_identity(body: str) -> None:
    parse_fragment(_frag(body))
    assert _claim_problems(_pending("code"), [_frag(body)]) == [
        "config.code: no changelog fragment names it"
    ]


def test_at5_prefix_collisions_claim_only_their_own_identity() -> None:
    live = _pending("code", "code_extra")
    assert _claim_problems(live, [_frag("Adds `config/code_extra`.")]) == [
        "config.code: no changelog fragment names it"
    ]
    assert _claim_problems(live, [_frag("Adds `config/code`.")]) == [
        "config.code_extra: no changelog fragment names it"
    ]


def test_at5_claims_are_read_from_changelog_d_fragments_only(tmp_path: Path) -> None:
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("Names `config/code`.\n")
    (tmp_path / "CHANGELOG.md").write_text(_frag("Adds `config/code`."))
    assert _claim_problems(_pending("code"), _fragment_texts(tmp_path)) != []
    (tmp_path / "changelog.d" / "probe.md").write_text(_frag("Adds `config/code`."))
    assert _claim_problems(_pending("code"), _fragment_texts(tmp_path)) == []


def test_at5_an_unreleased_identity_needs_no_minor_fixture() -> None:
    fixtures, live = _synthetic()
    live["entries"].append({"namespace": "ns", "code": "p", "since": "unreleased", "summary": "S"})
    assert minor_fixture_violations(fixtures, live, "7.1.0") == []

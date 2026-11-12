"""P1: the manifest policy module keeps the old messages and adds the schema-root and summary rules.

Synthetic manifests only (version 7.x), so nothing here moves when the live registry does.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.release.reason_manifest_policy import (
    append_only_violations,
    schema_root_violations,
    summary_version_violations,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release" / "reason_release_pin.py"


def _ent(code: str, since: str = "7.1.0") -> dict:
    return {
        "namespace": "ns", "code": code, "symbol": code.upper(), "summary": "S " + code,
        "owner": "O", "kind": "failure", "context": [], "since": since,
        "rules": [{"phase": "run", "severity": "error", "outcome": "fail", "retry": "never"}],
        "deprecated_in": None, "replaced_by": None,
    }  # fmt: skip


BASE = {
    "registry_version": "7.1.0",
    "reason_schema_version": 1,
    "event_schema_version": 1,
    "entries": [_ent("a"), _ent("b"), _ent("c")],
}

# Lists recorded once from the base-tree functions (origin/main, 41a86f47 line) on BASE.
APPEND_ONLY = {
    "pop": (lambda m: m["entries"].pop(0), ["deleted identity ('ns', 'a')"]),
    "retry": (
        lambda m: m["entries"][1]["rules"][0].__setitem__("retry", "x"),
        ["('ns', 'b'): rules changed (retry/outcome/phase semantics are frozen)"],
    ),
    "kind": (
        lambda m: m["entries"][1].__setitem__("kind", "warning"),
        ["('ns', 'b'): kind changed (retry/outcome/phase semantics are frozen)"],
    ),
    "summary": (
        lambda m: m["entries"][1].__setitem__("summary", "Edited."),
        ["a fixed safe summary changed without a registry version bump"],
    ),
    "repl": (
        lambda m: m["entries"][1].__setitem__("replaced_by", ["ns", "c"]),
        ["ns.b: replacement without deprecation"],
    ),
    "badrepl": (
        lambda m: m["entries"][1].update(deprecated_in="7.1.0", replaced_by=["nowhere", "x"]),
        ["ns.b: replacement ('nowhere', 'x') is not an identity"],
    ),
    "back": (lambda m: m.__setitem__("registry_version", "7.0.0"), ["registry_version went backwards"]),
    "add": (
        lambda m: m["entries"].append(_ent("d")),
        ["identities were added without a registry minor version bump"],
    ),
    "symbol": (lambda m: m["entries"][1].__setitem__("symbol", "ZZ"), ["('ns', 'b'): symbol changed"]),
}  # fmt: skip
RELEASE_PIN = {
    "add": (
        lambda m: m["entries"].append(_ent("d")),
        [
            "the catalog released as 7.1.0 changed without a registry version bump",
            "4 identities claim an already-released since 7.1.0 (released with 3)",
        ],
    ),
    "summary": (
        lambda m: m["entries"][0].__setitem__("summary", "Edited."),
        ["the catalog released as 7.1.0 changed without a registry version bump"],
    ),
    "released_since": (
        lambda m: (m.__setitem__("registry_version", "7.9.0"), m["entries"].append(_ent("d"))),
        ["4 identities claim an already-released since 7.1.0 (released with 3)"],
    ),
    "back": (
        lambda m: m.__setitem__("registry_version", "7.0.1"),
        [
            "registry_version went backwards (7.0.1 < 7.1.0)",
            "3 identities claim since 7.1.0, newer than the registry",
        ],
    ),
}


@pytest.mark.parametrize("name", sorted(APPEND_ONLY))
def test_append_only_messages_are_byte_identical(name: str) -> None:
    mutate, want = APPEND_ONLY[name]
    live = copy.deepcopy(BASE)
    mutate(live)
    assert append_only_violations(BASE, live) == want


def test_a_tombstone_change_is_reported_and_a_new_tombstone_is_not() -> None:
    live = copy.deepcopy(BASE)
    live["entries"][1]["deprecated_in"] = "7.1.0"
    assert append_only_violations(BASE, live) == []
    pinned = copy.deepcopy(live)
    live["entries"][1]["deprecated_in"] = "7.1.5"
    assert append_only_violations(pinned, live) == ["('ns', 'b'): tombstone changed"]


@pytest.mark.parametrize("name", sorted(RELEASE_PIN))
def test_legacy_v1_pin_messages_are_byte_identical(name: str) -> None:
    mutate, want = RELEASE_PIN[name]
    live = copy.deepcopy(BASE)
    mutate(live)
    assert pol.legacy_v1_violations(_v1(BASE), live) == want


def test_schema_root_violations() -> None:
    assert schema_root_violations(BASE, copy.deepcopy(BASE)) == []
    for root in ("reason_schema_version", "event_schema_version"):
        other = copy.deepcopy(BASE)
        other[root] = 2
        assert schema_root_violations(BASE, other) == [f"{root} differs (1 vs 2)"]


def test_summary_version_violations() -> None:
    def run(text: str) -> list[str]:
        return summary_version_violations([{**_ent("a"), "summary": text}])

    assert summary_version_violations(BASE["entries"]) == []
    assert run("Added at registry 9.9.0.") == ["ns.a: summary names a version literal"]
    assert run("Added at 9.9.0") == ["ns.a: summary names a version literal"]
    for ok in ("Python 3.10 only.", "Build 9.9.0.1", "Release 9.9.0beta"):
        assert run(ok) == [], ok


@pytest.mark.parametrize("cwd", [ROOT, None])
def test_release_check_command_runs_by_file_path(cwd: Path | None, tmp_path: Path) -> None:
    # The command release_checks.sh runs: by file path, so only scripts/release is on sys.path.
    run_dir = ROOT if cwd else tmp_path
    res = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(SCRIPT) if not cwd else "scripts/release/reason_release_pin.py", "--check"],
        cwd=run_dir, capture_output=True, text=True, check=False,
    )  # fmt: skip
    # Between releases the pin is stale by design (exit 1); only an import failure is a defect.
    assert res.returncode in (0, 1), res.stderr[-500:]
    assert "reason release pin" in res.stdout + res.stderr, res.stderr[-500:]


# REG-RELEASE E2: pending declarations, the per-field v2 pin, the ceiling and the transitions.
# Synthetic manifests use the real 1.22 to 1.24 numbers where the ceiling rule is under test.
from scripts.release import reason_manifest_policy as pol  # noqa: E402
from scripts.release import reason_release_pin as rp  # noqa: E402

PENDING = "unreleased"


def _rel() -> dict:
    """Shipped 1.22.0 (identity a), live 1.24.0 (b, then c, each under an old per-PR minor)."""
    return {
        "registry_version": "1.24.0",
        "reason_schema_version": 1,
        "event_schema_version": 1,
        "entries": [_ent("a", "1.22.0"), _ent("b", "1.23.0"), _ent("c", "1.24.0")],
    }


def _rel_fixtures() -> dict[str, dict]:
    e = _rel()["entries"]
    return {
        "reason-manifest-v1.22-a.json": {"registry_version": "1.22.0", "entries": [e[0]]},
        "reason-manifest-v1.23-b.json": {"registry_version": "1.23.0", "entries": [e[1]]},
        "reason-manifest-v1.24-c.json": {"registry_version": "1.24.0", "entries": [e[2]]},
    }


def _by(m: dict, code: str) -> dict:
    return next(e for e in m["entries"] if e["code"] == code)


def _deprecate(m: dict) -> str:
    _by(m, "b").update(deprecated_in=PENDING, replaced_by=["ns", "c"])
    return "b"


def _resummarize(m: dict) -> str:
    _by(m, "a")["summary"] = "S a, corrected"
    return "a"


EDITS = [(_deprecate, frozenset()), (_resummarize, frozenset({("ns", "a")}))]


def test_at1_an_unreleased_identity_passes_every_ordinary_gate() -> None:
    live = copy.deepcopy(BASE)
    live["entries"].append(_ent("d", PENDING))
    pin = rp.build_pin(BASE, "7.1.0")
    assert live["registry_version"] == pol.allowed_registry_version(pin)
    assert append_only_violations(BASE, live) == []
    assert pol.since_floor_violations(BASE, live) == []
    assert pol.since_window_violations([("ns.d", PENDING)], "7.0.0", "7.1.0") == []
    fixtures = {
        "reason-manifest-v7.1-x.json": {"registry_version": "7.1.0", "entries": BASE["entries"]}
    }
    assert pol.minor_fixture_violations(fixtures, live, "7.0.0") == []
    assert pol.release_pin_violations(pin, live) == []
    assert pol.unreleased_violations(live, pin) == []


def _first(p: dict) -> dict:
    return p["identities"][0]


FORMAT_CASES = {
    **{f"missing-{r}": (lambda p, r=r: p.pop(r), f"root {r} is missing") for r in (
        "format_version", "registry_version", "baseline_ceiling", "identities", "since_counts")},
    "extra-root": (lambda p: p.__setitem__("manifest_sha256", "0" * 64), "root manifest_sha256 is not allowed"),
    "format-1": (lambda p: p.__setitem__("format_version", 1), "format_version"),
    "format-bool": (lambda p: p.__setitem__("format_version", True), "format_version"),
    "format-string": (lambda p: p.__setitem__("format_version", "2"), "format_version"),
    "version-int": (lambda p: p.__setitem__("registry_version", 7), "registry_version"),
    "ceiling-short": (lambda p: p.__setitem__("baseline_ceiling", "7.1"), "baseline_ceiling"),
    "identities-count": (lambda p: p.__setitem__("identities", 3), "identities must be an array"),
    "identities-object": (lambda p: p.__setitem__("identities", {}), "identities must be an array"),
    "record-not-object": (lambda p: p["identities"].__setitem__(0, ["ns", "a"]), "identity record 0"),
    "record-missing-key": (lambda p: _first(p).pop("fields"), "identity record 0"),
    "record-extra-key": (lambda p: _first(p).__setitem__("since", "7.1.0"), "identity record 0"),
    "empty-namespace": (lambda p: _first(p).__setitem__("namespace", ""), "identity record 0"),
    "empty-code": (lambda p: _first(p).__setitem__("code", ""), "identity record 0"),
    "code-int": (lambda p: _first(p).__setitem__("code", 5), "identity record 0"),
    "duplicate": (lambda p: p["identities"].__setitem__(1, copy.deepcopy(_first(p))), "ns.a is duplicated"),
    "unsorted": (lambda p: p["identities"].reverse(), "not sorted"),
    "missing-field": (lambda p: _first(p)["fields"].pop("summary"), "ns.a fields"),
    "unknown-field": (lambda p: _first(p)["fields"].__setitem__("note", "0" * 64), "ns.a fields"),
    "fields-list": (lambda p: _first(p).__setitem__("fields", []), "ns.a fields"),
    "digest-upper": (lambda p: _first(p)["fields"].__setitem__("summary", "A" * 64), "ns.a field summary"),
    "digest-short": (lambda p: _first(p)["fields"].__setitem__("summary", "a" * 63), "ns.a field summary"),
    "digest-int": (lambda p: _first(p)["fields"].__setitem__("summary", 0), "ns.a field summary"),
    "counts-list": (lambda p: p.__setitem__("since_counts", []), "since_counts must be an object"),
    "counts-key": (lambda p: p.__setitem__("since_counts", {"x": 3}), "since_counts key"),
    "counts-bool": (lambda p: p.__setitem__("since_counts", {"7.1.0": True}), "since_counts 7.1.0"),
    "counts-string": (lambda p: p.__setitem__("since_counts", {"7.1.0": "3"}), "since_counts 7.1.0"),
    "counts-negative": (lambda p: p.__setitem__("since_counts", {"7.1.0": 4, "7.0.0": -1}), "since_counts 7.0.0"),
    "counts-total": (lambda p: p.__setitem__("since_counts", {"7.1.0": 2}), "since_counts total"),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(FORMAT_CASES))
def test_at1_the_strict_v2_reader_rejects_a_malformed_pin(name: str) -> None:
    mutate, needle = FORMAT_CASES[name]
    pin = rp.build_pin(BASE, "7.1.0")
    assert rp.pin_format_violations(pin) == []
    mutate(pin)
    problems = rp.pin_format_violations(pin)
    assert any(needle in p for p in problems), problems
    assert rp.pin_violations(pin, BASE) != []
    assert rp.load_pin(rp.pin_text(pin))[0] is None


def test_at1_the_reader_rejects_a_duplicate_key_and_non_json() -> None:
    text = rp.pin_text(rp.build_pin(BASE, "7.1.0"))
    doubled = text.replace('"code": "a",', '"code": "a",\n      "code": "a",', 1)
    assert doubled != text
    pin, problems = rp.load_pin(doubled)
    assert pin is None and any("duplicate key code" in p for p in problems), problems
    assert rp.load_pin("{")[0] is None
    assert rp.load_pin(text) == (json.loads(text), [])


@pytest.mark.parametrize(
    ("counts", "needle"),
    [({"7.0.0": 1, "7.1.0": 2}, "since_counts"), ({"7.1.0": 3}, "since_counts"),
     ({"7.0.0": 1, "7.1.0": 1, "6.0.0": 1}, "since_counts"), ({"7.0.0": 2, "7.1.0": 1}, "since_counts")],
)  # fmt: skip
def test_at1_since_counts_must_count_the_captured_identities(counts: dict, needle: str) -> None:
    live = copy.deepcopy(BASE)
    live["entries"][2]["since"] = "7.0.0"
    pin = rp.build_pin(live, "7.1.0")
    assert pin["since_counts"] == {"7.0.0": 1, "7.1.0": 2}
    assert pol.release_pin_violations(pin, live) == []
    if counts == pin["since_counts"]:
        return
    pin["since_counts"] = counts
    assert any(needle in p for p in pol.release_pin_violations(pin, live))


def _v1(m: dict) -> dict:
    """The retired aggregate pin, written the way the old --write wrote it."""
    counts = {}
    for e in m["entries"]:
        counts[e["since"]] = counts.get(e["since"], 0) + 1
    return {
        "registry_version": m["registry_version"],
        "manifest_sha256": pol.field_digest(m),
        "identities": len(m["entries"]),
        "since_counts": counts,
    }


def test_at1_migration_keeps_the_shipped_version_and_baselines_through_the_ceiling() -> None:
    shipped = _rel()
    shipped["registry_version"] = "1.22.0"
    shipped["entries"] = shipped["entries"][:1]
    pin, problems = rp.migrate_v1(_v1(shipped), _rel(), _rel_fixtures(), "1.21.0")
    assert problems == [] and pin is not None
    assert pin["registry_version"] == "1.22.0"
    assert pin["baseline_ceiling"] == pol.LAST_PER_PR_MINOR == "1.24.0"
    assert [(r["namespace"], r["code"]) for r in pin["identities"]] == [("ns", c) for c in "abc"]
    assert pin["since_counts"] == {"1.22.0": 1, "1.23.0": 1, "1.24.0": 1}
    assert pol.release_pin_violations(pin, _rel()) == []


def test_at1_migration_refuses_a_v1_or_full_fixture_mismatch() -> None:
    shipped = _rel()
    shipped["registry_version"] = "1.22.0"
    shipped["entries"] = shipped["entries"][:1]
    bad_v1 = _v1(shipped)
    bad_v1["since_counts"]["1.22.0"] = 2
    pin, problems = rp.migrate_v1(bad_v1, _rel(), _rel_fixtures(), "1.21.0")
    assert pin is None and problems
    fixtures = _rel_fixtures()
    fixtures["reason-manifest-v1.23-b.json"]["entries"][0]["summary"] = "Old wording."
    pin, problems = rp.migrate_v1(_v1(shipped), _rel(), fixtures, "1.21.0")
    assert pin is None and any("differs from the live entry" in p for p in problems), problems


@pytest.mark.parametrize(("edit", "pending"), EDITS)
def test_at1b_a_declared_pending_edit_passes_ordinary_gates_but_not_release_ready(
    edit, pending
) -> None:
    pin = rp.build_pin(_rel(), "1.22.0")
    live = _rel()
    edit(live)
    assert pol.release_pin_violations(pin, live, pending) == []
    assert append_only_violations(_rel(), live, pending) == []
    assert pol.minor_fixture_violations(_rel_fixtures(), live, "1.21.0") == []
    assert pol.release_ready_violations(live, pending) != []


def test_at1b_the_same_edits_without_markers_fail() -> None:
    pin = rp.build_pin(_rel(), "1.22.0")
    live = _rel()
    _resummarize(live)
    assert pol.release_pin_violations(pin, live) == ["ns.a: summary differs from the release pin"]
    live = _rel()
    _by(live, "b").update(deprecated_in="1.24.1", replaced_by=["ns", "c"])
    problems = pol.release_pin_violations(pin, live)
    assert "ns.b: deprecated_in differs from the release pin" in problems, problems


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "warning"), ("rules", []), ("owner", "P"), ("context", ["x"]),
     ("since", "1.21.0"), ("symbol", "Z")],
)  # fmt: skip
@pytest.mark.parametrize(("edit", "pending"), EDITS)
def test_at1b_every_other_field_stays_frozen_under_a_pending_edit(
    edit, pending, field, value
) -> None:
    pin = rp.build_pin(_rel(), "1.22.0")
    live = _rel()
    code = edit(live)
    _by(live, code)[field] = value
    assert f"ns.{code}: {field} differs from the release pin" in pol.release_pin_violations(
        pin, live, pending
    )


def test_at1b_a_numeric_tombstone_cannot_be_retargeted() -> None:
    shipped = _rel()
    _by(shipped, "c").update(deprecated_in="1.24.0", replaced_by=["ns", "a"])
    pin = rp.build_pin(shipped, "1.22.0")
    for change in (
        {"deprecated_in": PENDING, "replaced_by": ["ns", "b"]},
        {"replaced_by": ["ns", "b"]},
    ):
        live = copy.deepcopy(shipped)
        _by(live, "c").update(change)
        assert any(p.startswith("ns.c: ") for p in pol.release_pin_violations(pin, live)), change


@pytest.mark.parametrize(("edit", "pending"), EDITS)
def test_at1b_a_patch_stamp_moves_the_registry_and_keeps_the_ceiling(edit, pending) -> None:
    live = _rel()
    code = edit(live)
    t = pol.registry_transition(live, rp.build_pin(_rel(), "1.22.0"), pending)
    assert (t.kind, t.version, t.writes_fixture) == ("patch", "1.24.1", False)
    stamped = pol.stamped_manifest(live, t)
    new = rp.build_pin(stamped, t.version)
    assert (new["registry_version"], new["baseline_ceiling"]) == ("1.24.1", "1.24.0")
    assert stamped["registry_version"] == "1.24.1"
    assert _by(stamped, code)["deprecated_in"] in (None, "1.24.1")
    assert pol.release_pin_violations(new, stamped) == []
    assert pol.release_ready_violations(stamped, frozenset()) == []


def test_at2_numeric_fields_are_frozen_per_field_and_pending_additions_are_not() -> None:
    pin = rp.build_pin(BASE, "7.1.0")
    assert pol.release_pin_violations(pin, BASE) == []
    live = copy.deepcopy(BASE)
    live["entries"][0]["summary"] = "Edited."
    assert pol.release_pin_violations(pin, live) == ["ns.a: summary differs from the release pin"]
    live = copy.deepcopy(BASE)
    live["entries"][1]["rules"][0]["retry"] = "x"
    assert pol.release_pin_violations(pin, live) == ["ns.b: rules differs from the release pin"]
    live = copy.deepcopy(BASE)
    live["entries"].append(_ent("d", PENDING))
    assert pol.release_pin_violations(pin, live) == []
    assert ("ns", "d") not in {
        (r["namespace"], r["code"]) for r in rp.build_pin(live, "7.1.0")["identities"]
    }
    live = copy.deepcopy(BASE)
    live["entries"].append(_ent("d"))
    assert pol.release_pin_violations(pin, live) == [
        "ns.d: numeric identity is absent from the release pin"
    ]


@pytest.mark.parametrize(
    ("shipped", "allowed"),
    [("1.22.0", "1.24.0"), ("1.0.0", "1.24.0"), ("1.23.0", "1.24.0"), ("1.24.0", "1.24.0"),
     ("1.24.1", "1.24.1"), ("1.25.0", "1.25.0")],
)  # fmt: skip
def test_at3_the_allowed_version_is_the_shipped_version_or_the_last_per_pr_minor(
    shipped: str, allowed: str
) -> None:
    assert pol.allowed_registry_version({"registry_version": shipped}) == allowed


def test_at3_only_unreleased_additions_pass_the_ceiling() -> None:
    pin = rp.build_pin(_rel(), "1.22.0")
    assert pol.unreleased_violations(_rel(), pin) == []
    live = _rel()
    live["entries"].append(_ent("d", PENDING))
    assert pol.unreleased_violations(live, pin) == []
    assert pol.release_pin_violations(pin, live) == []
    live = _rel()
    live["entries"].append(_ent("d", "1.25.0"))
    assert pol.unreleased_violations(live, pin) != []
    for version in ("1.25.0", "1.23.0", "1.22.0", "1.24.1"):
        live = _rel()
        live["registry_version"] = version
        assert pol.unreleased_violations(live, pin) != [], version
        assert pol.release_pin_violations(pin, live) != [], version
    assert pol.unreleased_violations(_rel(), pin, ["reason-manifest-v1.25-x.json"]) != []
    assert (
        pol.unreleased_violations(_rel(), pin, ["reason-manifest-v1.24-fpe-fallbacks.json"]) == []
    )

"""P1: the manifest policy module keeps the old messages and adds the schema-root and summary rules.

Synthetic manifests only (version 7.x), so nothing here moves when the live registry does.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.release.reason_manifest_policy import (
    append_only_violations,
    release_pin_violations,
    schema_root_violations,
    summary_version_violations,
)
from scripts.release.reason_release_pin import release_pin

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
def test_release_pin_messages_are_byte_identical(name: str) -> None:
    mutate, want = RELEASE_PIN[name]
    live = copy.deepcopy(BASE)
    mutate(live)
    assert release_pin_violations(release_pin(BASE), live) == want


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

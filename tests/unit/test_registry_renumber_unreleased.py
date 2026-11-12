"""AT-10: `registry_renumber.py --to-unreleased` converts an in-flight identity slice to pending.

The cases share the scratch-repository helpers and fixtures of ``test_registry_renumber``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from scripts.release.reason_manifest_policy import entries_since

from tests.unit import test_registry_renumber as base
from tests.unit.test_registry_renumber import (
    CONTRACT,
    F29,
    PLAT,
    REG,
    add_code,
    commit,
    edit,
    fix_name,
    git,
    live,
    mver,
    nxt,
    out,
    regen_contract,
    run,
)

# The scratch-repository fixtures are defined once in the base module and registered here by name.
seed = base.seed
repo = base.repo

CONF2 = "src/decoy_engine/reasons/config/_part2.py"
CONTRACT_PY = "tests/unit/progress/test_consumer_contract.py"
NO_REASON = '"an_old_engine_with_no_reason": None,'
MARKERS = "PENDING_SUMMARY_EDITS: frozenset[tuple[str, str]] = frozenset()"


def _claimed_branch(repo: Path, v: str) -> str:
    """An in-flight identity-addition slice that claimed minor ``v`` on every carrier (AT-10)."""
    git(repo, "checkout", "-q", "-b", "a", "main")
    add_code(repo, CONF2, "probe_a", v)
    edit(repo, REG, lambda t: re.sub(r'(?m)^(REGISTRY_VERSION = ")[^"]*"', rf'\g<1>{v}"', t))
    edit(
        repo, "docs/reasons.md", lambda t: re.sub(r"(`registry_version` \(`)[^`]*", rf"\g<1>{v}", t)
    )
    edit(repo, "docs/reasons.md", lambda t: t + f"\nThe probe `config/probe_a` lands at `{v}`.\n")
    (repo / "tests/unit/test_probe_a.py").write_text(
        f'EXPECTED = {{"since": "{v}"}}\nOTHER = \'{v}\'\n'
    )
    (repo / "tests/fixtures/probe").mkdir(parents=True)
    (repo / "tests/fixtures/probe/a.json").write_text(json.dumps({"since": v}) + "\n")
    (repo / "docs/records").mkdir(exist_ok=True)
    (repo / "docs/records/probe-a.md").write_text(f'Registry: "{v}".\nThe slice lands at {v}.\n')
    (repo / "changelog.d").mkdir(exist_ok=True)
    (repo / "changelog.d/probe-a.md").write_text(
        f"### Added (probe, A, 2026-11-10)\n\nAdds `config/probe_a` at `{v}`.\n"
    )
    edit(
        repo,
        CONTRACT_PY,
        lambda t: t.replace(NO_REASON, NO_REASON + '\n        "a_branch_fact": None,'),
    )
    body = {k: x for k, x in live(repo).items() if k != "entries"}
    body["entries"] = entries_since(live(repo), v)
    name = f"{F29}/{fix_name(v, 'a')}"
    (repo / name).write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    regen_contract(repo)
    commit(repo, "slice a")
    return name


def test_to_unreleased_converts_an_identity_addition_branch(repo):  # AT-10
    m0 = mver(repo)
    v = nxt(m0)
    fixture = _claimed_branch(repo, v)
    res = run(repo, "--to-unreleased")
    assert res.returncode == 0, out(res)
    cat = (repo / CONF2).read_text()
    assert (
        "since=UNRELEASED)" in cat and "from decoy_engine.reasons._types import UNRELEASED\n" in cat
    )
    assert v not in cat and 'since="unreleased"' not in cat and "since='unreleased'" not in cat
    data = (repo / "tests/unit/test_probe_a.py").read_text()
    assert data == 'EXPECTED = {"since": "unreleased"}\nOTHER = \'unreleased\'\n'
    assert json.loads((repo / "tests/fixtures/probe/a.json").read_text()) == {"since": "unreleased"}
    doc = (repo / "docs/reasons.md").read_text()
    assert (
        f"`registry_version` (`{m0}`)" in doc and "`config/probe_a` lands at `unreleased`." in doc
    )
    rec = (repo / "docs/records/probe-a.md").read_text()
    assert rec == 'Registry: "unreleased".\nThe slice lands at unreleased.\n'
    frag = (repo / "changelog.d/probe-a.md").read_text()
    assert frag.endswith("Adds `config/probe_a` at `unreleased`.\n")
    doc_line = next(x for x in doc.splitlines() if "`config/probe_a`" in x)
    for text in (data, doc_line, rec, frag, (repo / "tests/fixtures/probe/a.json").read_text()):
        assert "UNRELEASED" not in text and v not in text
    assert v not in doc
    assert mver(repo) == m0 and not (repo / fixture).exists()
    changed = set(git(repo, "diff", "--name-only", "HEAD", "--", F29).split())
    assert changed == {fixture, CONTRACT}
    contract = json.loads((repo / CONTRACT).read_text())
    assert "a_branch_fact" in contract["degraded_readings"]
    assert {
        x for x in re.findall(r'"registry_version": "([^"]*)"', (repo / CONTRACT).read_text())
    } == {m0}
    probe = next(e for e in live(repo)["entries"] if e["code"] == "probe_a")
    assert probe["since"] == "unreleased"
    commit(repo, "to unreleased")
    res = run(repo, "--check")
    assert res.returncode == 0, out(res)


def _patch(version: str) -> str:
    major, minor, _ = version.split(".")
    return f"{major}.{minor}.1"


def _first_deprecation(repo: Path, m0: str) -> None:
    doc = '"The pipeline configuration failed schema validation.", since="1.5.0"'
    edit(
        repo,
        PLAT,
        lambda t: t.replace(
            doc, doc + f', deprecated_in="{_patch(m0)}", replaced_by=("platform", "job_failed")'
        ),
    )
    edit(
        repo,
        REG,
        lambda t: t.replace(f'REGISTRY_VERSION = "{m0}"', f'REGISTRY_VERSION = "{_patch(m0)}"'),
    )


def _marked_summary(repo: Path, m0: str) -> None:
    edit(
        repo,
        PLAT,
        lambda t: t.replace("Job complete: failed. The server log has the details.", "Changed."),
    )
    edit(repo, REG, lambda t: t.replace(MARKERS, MARKERS[:-2] + '({("platform", "job_failed")})'))
    edit(
        repo,
        REG,
        lambda t: t.replace(f'REGISTRY_VERSION = "{m0}"', f'REGISTRY_VERSION = "{_patch(m0)}"'),
    )


def _mixed(repo: Path, m0: str) -> None:
    add_code(repo, CONF2, "probe_m", nxt(m0))
    edit(
        repo,
        PLAT,
        lambda t: t.replace("Job complete: failed. The server log has the details.", "Changed."),
    )
    edit(
        repo,
        REG,
        lambda t: t.replace(f'REGISTRY_VERSION = "{m0}"', f'REGISTRY_VERSION = "{nxt(m0)}"'),
    )


@pytest.mark.parametrize(
    ("setup", "keys"),
    [(_first_deprecation, ["edited-existing platform.v2_config_invalid", "version-without-identity"]),
     (_marked_summary, ["edited-existing platform.job_failed", "version-without-identity"]),
     (_mixed, ["edited-existing platform.job_failed"])],
)  # fmt: skip
def test_to_unreleased_refuses_an_edit_to_an_existing_identity(repo, setup, keys):  # AT-10
    m0 = mver(repo)
    git(repo, "checkout", "-q", "-b", "e", "main")
    setup(repo, m0)
    regen_contract(repo)
    commit(repo, "edit")
    res = run(repo, "--to-unreleased")
    assert res.returncode == 3 and all(k in out(res).split("\n") for k in keys), out(res)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.parametrize(
    ("rel", "line"),
    [("tests/unit/test_probe_v.py", 'assert manifest()["registry_version"] == "{v}"'),
     ("tests/unit/test_probe_v.py", "PIN = dict(registry_version='{v}', since='{v}')"),
     ("tests/unit/test_probe_v.py", 'assert manifest.get("registry_version") == "{v}"'),
     ("tests/unit/test_probe_v.py", 'assert manifest["registry_version"] != "{v}"'),
     ("tests/fixtures/probe/v.json", '{{"registry_version": "{v}", "since": "{v}"}}')],
)  # fmt: skip
def test_to_unreleased_refuses_a_registry_version_value_it_cannot_reset(repo, rel, line):  # AT-10
    # Only the two named anchors return to main's version; any other carrier of the registry
    # version would become the pending string, which a registry version never is.
    v = nxt(mver(repo))
    _claimed_branch(repo, v)
    (repo / rel).write_text(line.format(v=v) + "\n")
    commit(repo, "registry version carrier")
    res = run(repo, "--to-unreleased")
    assert res.returncode == 3 and f"unconverted {rel}:1" in out(res).split("\n"), out(res)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_to_unreleased_refuses_a_claim_that_is_not_above_main(repo):  # AT-10
    # Main already holds the claimed minor, so a branch line naming it may mean main's version.
    v = mver(repo)
    git(repo, "checkout", "-q", "-b", "a", "main")
    add_code(repo, CONF2, "probe_a", v)
    (repo / "tests/unit/test_probe_v.py").write_text(f'MAIN_IS_AT = "{v}"\n')
    regen_contract(repo)
    commit(repo, "claims main's version")
    res = run(repo, "--to-unreleased")
    assert res.returncode == 3 and out(res).startswith(f"claim-not-above-main {v} "), out(res)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_to_unreleased_leaves_the_worktree_alone_when_the_candidate_fails(repo):  # AT-10
    _claimed_branch(repo, nxt(mver(repo)))
    edit(repo, CONTRACT, lambda t: t.replace('"consumer_rule": "', '"consumer_rule": "x', 1))
    commit(repo, "stale contract")
    head = git(repo, "rev-parse", "HEAD")
    res = run(repo, "--to-unreleased")
    assert res.returncode == 4, out(res)
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(repo, "rev-parse", "HEAD") == head

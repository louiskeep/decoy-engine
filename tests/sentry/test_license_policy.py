"""License-policy sentry: NOTICE tracks uv.lock, every scoped license is allowed, and the
committed NOTICE agrees with the installed packages' own metadata.

Class: frozen-contract drift. The lock moves; the shipped NOTICE (a wheel license-file)
and the allowlist must move with it. scripts/dev/licenses.py is a pure function over
(lock, pyproject, metadata, policy), so the seeded fixtures below prove it bites: an AGPL
runtime dependency, an LGPL package excepted for extras only, a dev-only AGPL package
that must not count, and stale or hand-edited NOTICE text.
Pattern: port of decoy-platform tests/sentry/test_license_policy.py.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "license_gate"
_spec = importlib.util.spec_from_file_location("licenses_gate", ROOT / "scripts/dev/licenses.py")
assert _spec and _spec.loader
L = importlib.util.module_from_spec(_spec)
sys.modules["licenses_gate"] = L
_spec.loader.exec_module(L)

LOCK = (ROOT / "uv.lock").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
NOTICE = (ROOT / "NOTICE").read_text(encoding="utf-8")
POLICY = L.load_policy(ROOT / L.POLICY_PATH)
STRICT = os.environ.get("DECOY_LICENSE_STRICT") == "1"


def _fixture_policy() -> dict:
    pol = copy.deepcopy(POLICY)
    pol["extras"] = {"runtime": ["sftp"], "tooling": ["dev"]}
    pol["exceptions"]["lgplpkg"] = {
        "license": "LGPL-2.1",
        "scope": "extra",
        "reason": "fixture: optional, used unmodified, pip-installed as a separate library",
    }
    return pol


def _fixture_entries():
    pol = _fixture_policy()
    scope = L.lock_scope((FIX / "uv.lock").read_text(), pol)
    return scope, L.collect_entries(scope, pol, sites=[str(FIX / "dists")])


def test_fixture_scope_marks_runtime_and_extras_and_drops_tooling():
    scope, _ = _fixture_entries()
    assert scope == {
        ("childpkg", "0.1"): "runtime",
        ("evilpkg", "2.0"): "runtime",
        ("okpkg", "1.0"): "runtime",
        ("winonly", "1.0"): "runtime",
        ("lgplpkg", "1.0"): "extra: sftp",
    }


def test_seeded_agpl_runtime_dependency_is_a_violation():
    _, entries = _fixture_entries()
    bad = L.policy_violations(entries, _fixture_policy())
    assert [(v.name, v.license) for v in bad] == [("evilpkg", "AGPL-3.0-only")]


def test_extra_only_exception_does_not_cover_a_runtime_dependency():
    _, entries = _fixture_entries()
    promoted = [
        L.Entry(e.name, e.version, e.license, "runtime") if e.name == "lgplpkg" else e
        for e in entries
    ]
    bad = L.policy_violations(promoted, _fixture_policy())
    assert {(v.name, v.why) for v in bad} == {
        ("evilpkg", "disallowed license"),
        ("lgplpkg", "excepted for extras only"),
    }


def test_missing_metadata_fails_closed():
    pol = _fixture_policy()
    scope = L.lock_scope((FIX / "uv.lock").read_text(), pol)
    scope[("ghostpkg", "1.0")] = "runtime"
    with pytest.raises(L.UnclassifiedLicenseError, match="ghostpkg==1.0"):
        L.collect_entries(scope, pol, sites=[str(FIX / "dists")])


def test_marker_closure_follows_only_true_edges():
    closure = L.env_runtime_closure((FIX / "uv.lock").read_text())
    assert ("okpkg", "1.0") in closure and ("childpkg", "0.1") in closure
    assert (("winonly", "1.0") in closure) is (sys.platform == "win32")


def test_loosened_allowlist_is_a_policy_problem():
    loose = copy.deepcopy(POLICY)
    loose["allowed"].append("AGPL-3.0-only")
    assert L.policy_file_problems(loose, PYPROJECT)
    assert L.policy_file_problems(POLICY, PYPROJECT) == []


def test_unclassed_extra_is_a_policy_problem():
    added = PYPROJECT.replace(
        "[project.optional-dependencies]\n", "[project.optional-dependencies]\nnewx = []\n", 1
    )
    assert added != PYPROJECT
    assert any("newx" in p for p in L.policy_file_problems(POLICY, added))


@pytest.mark.parametrize(
    ("expr", "ok"),
    [
        ("MIT", True),
        ("Apache-2.0 OR BSD-3-Clause", True),
        ("MIT AND PSF-2.0", True),
        ("MIT AND AGPL-3.0-only", False),
        ("Apache-2.0 OR AGPL-3.0-only", True),
        ("GPL-3.0-only", False),
        ("SSPL-1.0", False),
        ("BUSL-1.1", False),
        ("FSL-1.1-MIT", False),
        ("LGPL-2.1", False),
        ("MPL-2.0", False),
    ],
)
def test_expression_evaluation(expr, ok):
    e = L.Entry("x", "1", expr, "runtime")
    assert (L.policy_violations([e], POLICY) == []) is ok


def test_exception_is_pinned_to_name_and_exact_license():
    assert (
        L.policy_violations([L.Entry("paramiko", "5.0.0", "LGPL-2.1", "extra: sftp")], POLICY) == []
    )
    assert L.policy_violations(
        [L.Entry("paramiko", "5.0.0", "AGPL-3.0-only", "extra: sftp")], POLICY
    )
    assert L.policy_violations([L.Entry("somelib", "1", "LGPL-2.1", "extra: sftp")], POLICY)
    # paramiko is accepted only while it stays behind the sftp extra
    assert L.policy_violations([L.Entry("paramiko", "5.0.0", "LGPL-2.1", "runtime")], POLICY)


def test_every_exception_is_justified_and_in_the_notice_callout():
    head = NOTICE.split(L.SEPARATOR)[0]
    assert POLICY["exceptions"], "the exception list must not be empty"
    for name, exc in POLICY["exceptions"].items():
        assert len(exc["reason"].split()) >= 8, name
        assert f"  {name} " in head, name
        assert "unmodified" in exc["reason"], name
    assert "vendored or modified copies are forbidden" in head


def test_unclassifiable_metadata_fails_closed():
    with pytest.raises(L.UnclassifiedLicenseError):
        L.classify({"License": "Some bespoke terms see website", "Classifier": []})


def test_override_applies_only_when_metadata_states_nothing():
    ovr = POLICY["overrides"]["google-crc32c==1.8.0"]
    assert L.classify({"License": None, "Classifier": []}, ovr) == "Apache-2.0"
    with pytest.raises(L.UnclassifiedLicenseError):
        L.classify({"License": "custom terms", "Classifier": []}, ovr)


def test_committed_notice_matches_the_lock():
    assert L.notice_problems(LOCK, PYPROJECT, NOTICE, POLICY) == []


def test_committed_notice_licenses_pass_the_policy():
    assert L.policy_violations(L.parse_notice(NOTICE), POLICY) == []


def test_committed_notice_content():
    entries = {e.name: e for e in L.parse_notice(NOTICE)}
    assert not set(entries) & {"pytest", "mypy", "ruff", "moto", "hypothesis", "sphinx", "mutmut"}
    assert entries["paramiko"].scope == "extra: sftp"
    assert entries["paramiko"].license == "LGPL-2.1"
    assert entries["python-stdnum"].scope == "runtime"
    assert entries["pandas"].scope == "runtime"
    assert "TODO(release)" not in NOTICE
    assert "OWNER ITEM" in NOTICE.split("\n")[1]


def test_stale_notice_mutants_are_caught():
    lines = NOTICE.splitlines(keepends=True)
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("  pandas=="))
    dropped = "".join(lines[:idx] + lines[idx + 1 :])
    assert L.notice_problems(LOCK, PYPROJECT, dropped, POLICY)
    bumped = NOTICE.replace(lines[idx], lines[idx].replace("==", "==9"), 1)
    assert L.notice_problems(LOCK, PYPROJECT, bumped, POLICY)
    rescoped = NOTICE.replace(lines[idx], lines[idx].replace("[runtime]", "[extra: ml]"), 1)
    assert L.notice_problems(LOCK, PYPROJECT, rescoped, POLICY)
    extra = NOTICE + "  pytest==9.0.0  MIT  [runtime]\n"
    assert L.notice_problems(LOCK, PYPROJECT, extra, POLICY)
    edited = NOTICE.replace("Named license exceptions", "License exceptions", 1)
    assert L.notice_problems(LOCK, PYPROJECT, edited, POLICY)


def test_stale_policy_exception_is_caught():
    pol = copy.deepcopy(POLICY)
    pol["exceptions"]["gonepkg"] = {
        "license": "LGPL-2.1",
        "scope": "extra",
        "reason": "fixture: a package no longer in the lock, used unmodified",
    }
    problems = L.notice_problems(LOCK, PYPROJECT, NOTICE, pol)
    assert any("gonepkg" in p for p in problems)


def test_lock_bump_makes_notice_stale():
    bumped = LOCK.replace('name = "pandas"\nversion = "', 'name = "pandas"\nversion = "9.', 1)
    assert bumped != LOCK
    assert L.notice_problems(bumped, PYPROJECT, NOTICE, POLICY)


def test_policy_json_shape():
    raw = json.loads((ROOT / L.POLICY_PATH).read_text())
    assert set(raw) == {"allowed", "exceptions", "overrides", "extras"}


# --- installed metadata ---------------------------------------------------------------


def _env_or_skip():
    env = L.verify_env(LOCK, NOTICE, POLICY)
    if env.drifted:
        msg = f"not a lock-exact venv: {len(env.drifted)} installed version(s) differ from uv.lock"
        if STRICT:
            pytest.fail(msg)
        pytest.skip(
            msg + "; run in the scripts/dev/bootstrap.sh venv or set DECOY_LICENSE_STRICT=1"
        )
    return env


def test_notice_matches_installed_metadata():
    env = _env_or_skip()
    assert env.problems == []
    assert env.required >= 30 and env.compared >= env.required


def test_wrong_license_in_notice_is_caught_against_installed_metadata():
    _env_or_skip()
    line = next(ln for ln in NOTICE.splitlines() if ln.startswith("  pandas=="))
    wrong = NOTICE.replace(line, line.replace("BSD-family-unspecified-clause", "MIT"), 1)
    assert wrong != NOTICE
    problems = L.verify_env(LOCK, wrong, POLICY).problems
    assert len(problems) == 1 and problems[0].startswith("pandas==")


def test_errors_name_pins_not_metadata_source_paths(tmp_path):
    canary = tmp_path / "CANARY-s3cr3t-site"
    canary.mkdir()
    pol = _fixture_policy()
    scope = L.lock_scope((FIX / "uv.lock").read_text(), pol)
    with pytest.raises(L.UnclassifiedLicenseError) as err:
        L.collect_entries(scope, pol, sites=[str(canary)])
    assert "okpkg==1.0" in str(err.value)
    assert "CANARY" not in str(err.value) and str(tmp_path) not in str(err.value)

"""A branch that adds reason identities bumps the registry exactly one minor over its merge base.

`test_reason_release_pin.py` compares the live catalog with the last RELEASED pin, so two
unreleased branches could each add identities at the same unreleased version and both pass. This
test compares with the catalog at `git merge-base HEAD origin/main` instead: when the branch adds
identities, `REGISTRY_VERSION` must be the base's next minor and every added identity must claim
it; no identity the base already had may change its `since`. No version literal lives here: both
sides are read from git and from the live registry.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest
from packaging.version import Version

from decoy_engine import reasons

REPO = Path(__file__).resolve().parents[2]
_REGISTRY = "src/decoy_engine/reasons/_registry.py"


def _git(*args: str) -> bytes:
    return subprocess.run(  # noqa: S603 -- static argv (git + literal subcommands + git shas)
        ["git", "-C", str(REPO), *args],  # noqa: S607 -- git from PATH, as the release scripts do
        check=True,
        capture_output=True,
        timeout=120,
    ).stdout


def _merge_base() -> str:
    try:
        _git("rev-parse", "--git-dir")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout: the merge-base registry bump rule is UNCHECKED")
    try:
        return _git("merge-base", "HEAD", "origin/main").decode().strip()
    except subprocess.CalledProcessError:
        pytest.fail("fetch origin/main (CI checks out with fetch-depth: 0)")


def _base_version(base: str) -> str:
    tree = ast.parse(_git("show", f"{base}:{_REGISTRY}").decode("utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and [getattr(t, "id", None) for t in node.targets] == ["REGISTRY_VERSION"]
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    raise AssertionError(f"REGISTRY_VERSION not found in {base}:{_REGISTRY}")


def _base_since(base: str, tmp: Path) -> dict[tuple[str, str], str]:
    with tarfile.open(fileobj=BytesIO(_git("archive", base, "src/decoy_engine"))) as tar:
        tar.extractall(tmp, filter="data")
    probe = (
        "import json, sys\n"
        "from decoy_engine import reasons\n"
        "json.dump([[e['namespace'], e['code'], e['since']] "
        "for e in reasons.manifest()['entries']], sys.stdout)\n"
    )
    out = subprocess.run(  # noqa: S603 -- static argv (sys.executable + a literal probe)
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(tmp / 'src')!r})\n" + probe,
        ],
        check=True,
        capture_output=True,
        timeout=300,
        cwd=tmp,
    ).stdout
    return {(ns, code): since for ns, code, since in json.loads(out)}


def _next_minor(version: str) -> str:
    v = Version(version)
    return f"{v.major}.{v.minor + 1}.0"


def bump_violations(
    base_version: str,
    base: dict[tuple[str, str], str],
    live_version: str,
    live: dict[tuple[str, str], str],
) -> list[str]:
    """Why ``live`` breaks the one-minor-per-branch rule against ``base``; empty when it holds."""
    problems = [
        f"{ns}.{code}: since changed {base[(ns, code)]} -> {since}"
        for (ns, code), since in live.items()
        if (ns, code) in base and base[(ns, code)] != since
    ]
    added = {key: since for key, since in live.items() if key not in base}
    if not added:
        if live_version != base_version:
            problems.append(f"registry moved {base_version} -> {live_version} with no new identity")
        return problems
    want = _next_minor(base_version)
    if live_version != want:
        problems.append(f"registry is {live_version}; adding identities needs {want}")
    problems.extend(
        f"{ns}.{code}: new identity claims since {since}, not {live_version}"
        for (ns, code), since in sorted(added.items())
        if since != live_version
    )
    return problems


def _live() -> dict[tuple[str, str], str]:
    return {(e["namespace"], e["code"]): e["since"] for e in reasons.manifest()["entries"]}


def test_registry_is_merge_base_plus_one_minor_and_new_identities_carry_it(
    tmp_path: Path,
) -> None:
    base = _merge_base()
    problems = bump_violations(
        _base_version(base), _base_since(base, tmp_path), reasons.REGISTRY_VERSION, _live()
    )
    assert problems == [], problems


_BASE = {("runtime", "a"): "1.0.0", ("runtime", "b"): "1.1.0"}


@pytest.mark.parametrize(
    ("live_version", "live", "expected"),
    [
        # the gate's seeded case: a new identity at the base's since, version unbumped
        ("1.1.0", {**_BASE, ("runtime", "c"): "1.1.0"}, "adding identities needs 1.2.0"),
        ("1.3.0", {**_BASE, ("runtime", "c"): "1.3.0"}, "adding identities needs 1.2.0"),
        ("1.1.1", {**_BASE, ("runtime", "c"): "1.1.1"}, "adding identities needs 1.2.0"),
        ("1.2.0", {**_BASE, ("runtime", "c"): "1.1.0"}, "claims since 1.1.0, not 1.2.0"),
        ("1.2.0", {**_BASE, ("runtime", "b"): "1.2.0"}, "since changed 1.1.0 -> 1.2.0"),
        ("1.2.0", dict(_BASE), "with no new identity"),
    ],
    ids=["unbumped", "two-minors", "patch", "stale-since", "moved-since", "bump-without-addition"],
)
def test_bump_violations_are_reported(
    live_version: str, live: dict[tuple[str, str], str], expected: str
) -> None:
    assert any(expected in p for p in bump_violations("1.1.0", _BASE, live_version, live))


def test_a_correct_bump_is_accepted() -> None:
    live = {**_BASE, ("runtime", "c"): "1.2.0", ("runtime", "d"): "1.2.0"}
    assert bump_violations("1.1.0", _BASE, "1.2.0", live) == []
    assert bump_violations("1.1.0", _BASE, "1.1.0", dict(_BASE)) == []

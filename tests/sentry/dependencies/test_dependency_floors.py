"""Sentry: advisory-backed dependency floors hold in `pyproject.toml` and in `uv.lock`.

A floor is the first release an advisory names as fixed; the lock is pinned independently, so
the contract is two inequalities (floor >= fixed release, locked version >= floor), the
`uv lock --check` idea written as a test. A routine lock upgrade never moves the floor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[3]

# GHSA-44vg-5wv2-h2hg / CVE-2026-32640: objects passed as names can leak modules or dangerous
# callables into the simpleeval sandbox; affected < 1.0.5, patched in 1.0.5.
SIMPLEEVAL_FIXED = Version("1.0.5")


def _floor(name: str) -> Version:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    [req] = [Requirement(d) for d in deps if Requirement(d).name == name]
    [spec] = [s for s in req.specifier if s.operator == ">="]
    return Version(spec.version)


def _locked(name: str) -> Version:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    [version] = [p["version"] for p in lock["package"] if p["name"] == name]
    return Version(version)


@pytest.mark.parametrize(
    "check",
    [
        pytest.param("floor_ge_1_0_5", id="floor_ge_1_0_5-FAIL_BEFORE"),
        pytest.param("lock_ge_floor", id="lock_ge_floor-CHARACTERIZATION"),
    ],
)
def test_simpleeval_floor_is_advisory_fixed_and_lock_satisfies_it(check: str) -> None:
    if check == "floor_ge_1_0_5":
        assert _floor("simpleeval") >= SIMPLEEVAL_FIXED
    else:
        assert _locked("simpleeval") >= _floor("simpleeval")

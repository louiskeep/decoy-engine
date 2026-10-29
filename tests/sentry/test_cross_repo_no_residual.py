"""Manifest-driven residual sentry for the removed STORM concept.

Scans every tracked path and file content, case-insensitively, for the `storm`
token, then checks an exact list of forbidden old symbols, schema strings,
config keys, routes, permissions, commands and environment-variable names (plus
the removed canary-check surface, which the case-insensitive scan cannot see).

Only the exceptions in `EXCEPTIONS` may keep an old literal: historical records
and the sentry itself. Each records its purpose and whether it can reach a
published artifact. The scanner is exercised against seeded violations in every
surface class so a passing run is never vacuous.
"""

from __future__ import annotations

import fnmatch
import pathlib
import re
import shutil
import subprocess
from dataclasses import dataclass

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

STORM = re.compile(r"storm", re.IGNORECASE)

FORBIDDEN_TOKENS: tuple[str, ...] = (
    # symbols, schemas, config keys
    "run_storm",
    "storm-post-mask",
    "storm_postmask",
    "compute_storm_postmask",
    "run_storm_post_mask",
    "StormPostMaskReport",
    "JobStormReport",
    "StormScan",
    "storm_run_group_id",
    "decoy_engine.storm",
    # wire and permission names
    "storm.scan",
    "storm.override",
    "storm.read_samples",
    "/api/storm",
    # environment variables
    "STORM_",
    "DECOY_STORM",
    # removed canary-check surface (not a `storm` token)
    "canary_check",
    "canary-check",
    "CanaryReport",
    "/canary-check",
)


@dataclass(frozen=True)
class Allowed:
    glob: str
    purpose: str
    reaches_published_artifact: bool


EXCEPTIONS: tuple[Allowed, ...] = (
    Allowed("docs/plans/*", "immutable plan history", False),
    Allowed("docs/archive/*", "archived records", False),
    Allowed("docs/decisions/*", "immutable ADRs", False),
    Allowed("docs/records/*", "immutable build/audit records", False),
    Allowed("CHANGELOG.md", "release history", True),
    Allowed("tests/sentry/test_cross_repo_no_residual.py", "sentry definition", False),
)


def _excepted(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, e.glob) for e in EXCEPTIONS)


def scan(root: pathlib.Path, rel_paths: list[str]) -> list[str]:
    """Return `path: reason` for every non-excepted residual in `rel_paths`."""
    hits: list[str] = []
    for rel in rel_paths:
        if _excepted(rel):
            continue
        if STORM.search(rel):
            hits.append(f"{rel}: path")
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if STORM.search(text):
            hits.append(f"{rel}: content")
        for token in FORBIDDEN_TOKENS:
            if token in text:
                hits.append(f"{rel}: forbidden token {token!r}")
    return hits


def _tracked() -> list[str]:
    out = subprocess.run(
        [shutil.which("git") or "git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return out.splitlines()


def test_no_residual_storm_surface_in_tracked_files() -> None:
    assert scan(ROOT, _tracked()) == []


SEEDS = {
    "path": ("src/pkg/storm_helper.py", "x = 1\n"),
    "python-identifier": ("src/pkg/mod.py", "def compute_storm_postmask():\n    pass\n"),
    "config-key": ("examples/p.yaml", "version: 2\nrun_storm: true\n"),
    "schema-string": ("docs/ref.md", "schema `storm-post-mask/v1`\n"),
    "env-var": (".github/workflows/ci.yml", "env:\n  DECOY_STORM_MODE: on\n"),
    "compose-env": ("deploy/compose.yml", "environment:\n  - STORM_URL=x\n"),
    "generated-openapi": ("docs/openapi.json", '{"paths": {"/api/storm/scans": {}}}\n'),
    "removed-canary": ("docs/api.md", "POST /canary-check returns CanaryReport\n"),
}


@pytest.mark.parametrize("surface", sorted(SEEDS))
def test_sentry_catches_seeded_violation(tmp_path: pathlib.Path, surface: str) -> None:
    rel, body = SEEDS[surface]
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    assert scan(tmp_path, [rel]) != []


def test_exceptions_are_exact_and_documented() -> None:
    for exc in EXCEPTIONS:
        assert exc.purpose and "*" not in exc.glob.rstrip("*")
    assert not _excepted("src/decoy_engine/anything.py")
    assert not _excepted("tests/unit/test_x.py")

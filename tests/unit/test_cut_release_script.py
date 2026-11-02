"""scripts/release/cut-release.sh refuses every state it must not tag from.

Each case builds a scratch repository with a bare `origin`, copies the real cut-release.sh into
it, and replaces the heavy checks (`release_checks.sh`: lock environment, version agreement,
reason pin, DP certified profile, SBOM) with a stub that records that it ran. So these tests pin
the script's own gatekeeping: branch, tree, head, tag, CHANGELOG, ordering, and the re-check
after the checks ran. The real checks are exercised by running the script itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release" / "cut-release.sh"
GIT = shutil.which("git") or "/usr/bin/git"
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-11-01

### Added
- thing

## [0.1.0] - 2026-10-01
"""

STUB_CHECKS = """#!/usr/bin/env bash
set -eu
echo "checks ran for $1" >> "$CHECKS_LOG"
if [ -n "${STUB_DIRTY:-}" ]; then echo x > "$(git rev-parse --show-toplevel)/$STUB_DIRTY"; fi
if [ -n "${STUB_PUSHURL:-}" ]; then git config remote.origin.pushurl "$STUB_PUSHURL"; fi
exit "${STUB_EXIT:-0}"
"""


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    proc = subprocess.run(  # noqa: S603 - fixed git executable, test-literal arguments
        [GIT, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )
    return proc.stdout.strip()


class Repo:
    def __init__(self, tmp: Path) -> None:
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        self.log = tmp / "checks.log"
        _git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        _git(tmp, "init", "-q", "-b", "main", str(self.work))
        w = self.work
        (w / "pyproject.toml").write_text('[project]\nname = "decoy-engine"\nversion = "0.2.0"\n')
        (w / "CHANGELOG.md").write_text(CHANGELOG)
        rel = w / "scripts" / "release"
        rel.mkdir(parents=True)
        shutil.copy2(SCRIPT, rel / "cut-release.sh")
        (rel / "release_checks.sh").write_text(STUB_CHECKS)
        (rel / "release_checks.sh").chmod(0o755)
        _git(w, "add", "-A")
        _git(w, "commit", "-q", "-m", "init")
        _git(w, "remote", "add", "origin", str(self.origin))
        _git(w, "push", "-q", "origin", "main")
        _git(w, "tag", "-a", "v0.1.0", "-m", "old", "HEAD")
        _git(w, "push", "-q", "origin", "refs/tags/v0.1.0")

    @property
    def head(self) -> str:
        return _git(self.work, "rev-parse", "HEAD")

    def commit(self, name: str, text: str) -> None:
        (self.work / name).write_text(text)
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", f"edit {name}")

    def run(self, *, sha: str | None = None, apply: bool = False, **extra: str):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.work.parent),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "CHECKS_LOG": str(self.log),
            "EXPECTED_REMOTE_RE": "origin\\.git$",
            **extra,
        }
        if apply:
            env["APPLY"] = "1"
        argv = [BASH, str(self.work / "scripts" / "release" / "cut-release.sh")]
        if sha is not None:
            argv.append(sha)
        return subprocess.run(argv, cwd=self.work, env=env, capture_output=True, text=True)  # noqa: S603

    def checks_ran(self) -> bool:
        return self.log.exists()

    def local_tag(self, tag: str) -> str:
        return _git(self.work, "tag", "-l", tag)

    def remote_tag(self, tag: str) -> str:
        return _git(self.work, "ls-remote", "--tags", "origin", f"refs/tags/{tag}")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def _refused(proc, needle: str) -> None:
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert needle in proc.stderr, proc.stderr


# --- the paths that act -----------------------------------------------------------------------


def test_dry_run_checks_and_tags_nothing(repo: Repo) -> None:
    proc = repo.run(sha=repo.head)
    assert proc.returncode == 0, proc.stderr
    assert "DRY RUN" in proc.stdout
    assert repo.checks_ran()
    assert repo.local_tag("v0.2.0") == "" and repo.remote_tag("v0.2.0") == ""


def test_apply_creates_and_pushes_an_annotated_tag_at_the_expected_sha(repo: Repo) -> None:
    sha = repo.head
    proc = repo.run(sha=sha, apply=True)
    assert proc.returncode == 0, proc.stderr
    assert _git(repo.work, "cat-file", "-t", "v0.2.0") == "tag"
    assert _git(repo.work, "rev-parse", "v0.2.0^{commit}") == sha
    assert repo.remote_tag("v0.2.0^{}").split()[0] == sha


# --- refusals: nothing runs, nothing is tagged ----------------------------------------------


def _nothing_happened(repo: Repo) -> None:
    assert repo.local_tag("v0.2.0") == "" and repo.remote_tag("v0.2.0") == ""


def test_refuses_another_branch(repo: Repo) -> None:
    _git(repo.work, "checkout", "-q", "-b", "feature")
    _refused(repo.run(sha=repo.head, apply=True), "not on main")
    assert not repo.checks_ran()
    _nothing_happened(repo)


def test_refuses_a_detached_head(repo: Repo) -> None:
    _git(repo.work, "checkout", "-q", "--detach")
    _refused(repo.run(sha=repo.head, apply=True), "not on main")
    _nothing_happened(repo)


@pytest.mark.parametrize("kind", ["modified", "staged", "untracked"])
def test_refuses_a_dirty_tree(repo: Repo, kind: str) -> None:
    if kind == "untracked":
        (repo.work / "stray.txt").write_text("x")
    else:
        (repo.work / "CHANGELOG.md").write_text(CHANGELOG + "\nedit\n")
        if kind == "staged":
            _git(repo.work, "add", "CHANGELOG.md")
    _refused(repo.run(sha=repo.head, apply=True), "working tree is not clean")
    assert not repo.checks_ran()
    _nothing_happened(repo)


def test_refuses_without_an_expected_sha(repo: Repo) -> None:
    _refused(repo.run(apply=True), "usage")
    _refused(repo.run(sha=repo.head[:12], apply=True), "full 40-character")
    _nothing_happened(repo)


def test_refuses_a_head_other_than_the_expected_sha(repo: Repo) -> None:
    old = repo.head
    repo.commit("x.txt", "x")
    _git(repo.work, "push", "-q", "origin", "main")
    _refused(repo.run(sha=old, apply=True), "HEAD is")
    _nothing_happened(repo)


def test_refuses_a_local_main_ahead_of_origin(repo: Repo) -> None:
    repo.commit("x.txt", "x")
    _refused(repo.run(sha=repo.head, apply=True), "origin/main")
    _nothing_happened(repo)


def test_refuses_a_local_main_behind_origin(repo: Repo, tmp_path: Path) -> None:
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(repo.origin), str(other))
    (other / "y.txt").write_text("y")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "y")
    _git(other, "push", "-q", "origin", "main")
    _refused(repo.run(sha=repo.head, apply=True), "origin/main")
    _nothing_happened(repo)


def test_refuses_a_tag_that_exists_locally(repo: Repo) -> None:
    _git(repo.work, "tag", "v0.2.0", "HEAD~0")
    _refused(repo.run(sha=repo.head, apply=True), "already exists")
    assert repo.remote_tag("v0.2.0") == ""


def test_refuses_a_tag_that_exists_only_on_the_remote(repo: Repo) -> None:
    _git(repo.work, "tag", "v0.2.0", "HEAD")
    _git(repo.work, "push", "-q", "origin", "refs/tags/v0.2.0")
    _git(repo.work, "tag", "-d", "v0.2.0")
    _refused(repo.run(sha=repo.head, apply=True), "already exists")


def test_refuses_a_version_not_newer_than_the_latest_tag(repo: Repo) -> None:
    _git(repo.work, "tag", "-a", "v0.3.0", "-m", "newer", "HEAD")
    _refused(repo.run(sha=repo.head, apply=True), "not newer than")
    _nothing_happened(repo)


def test_refuses_a_remote_that_is_not_the_engine_repository(repo: Repo) -> None:
    _refused(
        repo.run(sha=repo.head, apply=True, EXPECTED_REMOTE_RE="github\\.com[:/]elsewhere/"),
        "does not match",
    )
    _nothing_happened(repo)


@pytest.mark.parametrize(
    ("changelog", "needle"),
    [
        (CHANGELOG.replace("## [0.2.0] - 2026-11-01", "## [0.2.0]"), "dated section"),
        (CHANGELOG.replace("## [0.2.0] - 2026-11-01\n", ""), "dated section"),
        (CHANGELOG.replace("## [Unreleased]\n\n", ""), "dated section"),
    ],
)
def test_refuses_a_changelog_without_the_dated_section(
    repo: Repo, changelog: str, needle: str
) -> None:
    repo.commit("CHANGELOG.md", changelog)
    _git(repo.work, "push", "-q", "origin", "main")
    _refused(repo.run(sha=repo.head, apply=True), needle)
    _nothing_happened(repo)


def test_refuses_when_the_checks_fail(repo: Repo) -> None:
    _refused(repo.run(sha=repo.head, apply=True, STUB_EXIT="3"), "release checks failed")
    assert repo.checks_ran()
    _nothing_happened(repo)


def test_refuses_when_the_checks_leave_the_tree_dirty(repo: Repo) -> None:
    _refused(repo.run(sha=repo.head, apply=True, STUB_DIRTY="sbom.cdx.json"), "not clean")
    assert repo.checks_ran()
    _nothing_happened(repo)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_refuses_index_flags_that_hide_a_modified_file(repo: Repo, flag: str) -> None:
    # git status reports such a file clean while the checks would run its edited content.
    _git(repo.work, "update-index", flag, "CHANGELOG.md")
    (repo.work / "CHANGELOG.md").write_text(CHANGELOG + "\nedit\n")
    assert _git(repo.work, "status", "--porcelain") == ""
    _refused(repo.run(sha=repo.head, apply=True), "hides")
    assert not repo.checks_ran()
    _nothing_happened(repo)


# --- push destination: every URL a push can reach is the engine repository -------------------


def _fork(tmp_path: Path) -> Path:
    fork = tmp_path / "fork.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(fork))
    return fork


def _fork_untouched(fork: Path) -> None:
    assert _git(fork, "for-each-ref", "refs/tags") == ""


def test_refuses_a_pushurl_to_another_repository(repo: Repo, tmp_path: Path) -> None:
    # Fetch still reads the real origin, so every fetch-side check passes.
    fork = _fork(tmp_path)
    _git(repo.work, "config", "remote.origin.pushurl", str(fork))
    _refused(repo.run(sha=repo.head, apply=True), "push URL")
    assert not repo.checks_ran()
    _nothing_happened(repo)
    _fork_untouched(fork)


def test_refuses_a_push_insteadof_rewrite_to_another_repository(repo: Repo, tmp_path: Path) -> None:
    fork = _fork(tmp_path)
    _git(repo.work, "config", f"url.{fork}.pushInsteadOf", str(repo.origin))
    _refused(repo.run(sha=repo.head, apply=True), "push URL")
    _nothing_happened(repo)
    _fork_untouched(fork)


def test_refuses_a_second_push_url_to_another_repository(repo: Repo, tmp_path: Path) -> None:
    # A remote with several push URLs pushes to all of them.
    fork = _fork(tmp_path)
    _git(repo.work, "config", "--add", "remote.origin.pushurl", str(repo.origin))
    _git(repo.work, "config", "--add", "remote.origin.pushurl", str(fork))
    _refused(repo.run(sha=repo.head, apply=True), "push URL")
    _nothing_happened(repo)
    _fork_untouched(fork)


def test_refuses_a_second_fetch_url_to_another_repository(repo: Repo, tmp_path: Path) -> None:
    # Without a pushurl, push goes to every `url` of the remote.
    fork = _fork(tmp_path)
    _git(repo.work, "config", "--add", "remote.origin.url", str(fork))
    _refused(repo.run(sha=repo.head, apply=True), "does not match")
    _nothing_happened(repo)
    _fork_untouched(fork)


def test_refuses_a_pushurl_the_checks_install(repo: Repo, tmp_path: Path) -> None:
    # The checks run code from the tree, so the destination is verified again after them.
    fork = _fork(tmp_path)
    _refused(repo.run(sha=repo.head, apply=True, STUB_PUSHURL=str(fork)), "push URL")
    assert repo.checks_ran()
    _nothing_happened(repo)
    _fork_untouched(fork)


def test_accepts_a_matching_pushurl(repo: Repo) -> None:
    _git(repo.work, "config", "remote.origin.pushurl", str(repo.origin))
    proc = repo.run(sha=repo.head, apply=True)
    assert proc.returncode == 0, proc.stderr
    assert repo.remote_tag("v0.2.0^{}").split()[0] == repo.head

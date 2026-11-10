"""`release_checks.sh` refuses a release while `changelog.d/` still holds fragments.

This catches a fragment that merges between the release-prep PR (which compiles them) and the
tag. The case runs the real script in a scratch repository; the check sits before any `uv` step,
so a refusal needs no lock or network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "release" / "release_checks.sh"
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git") or "/usr/bin/git"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _scratch(tmp_path: Path, files: dict[str, str]) -> Path:
    work = tmp_path / "work"
    rel = work / "scripts" / "release"
    rel.mkdir(parents=True)
    shutil.copy2(SCRIPT, rel / "release_checks.sh")
    (work / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    subprocess.run([GIT, "init", "-q", str(work)], check=True)  # noqa: S603
    return work


def _run(work: Path) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"], "HOME": str(work.parent)}
    return subprocess.run(  # noqa: S603
        [BASH, str(work / "scripts" / "release" / "release_checks.sh"), "9.9.9"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )


def test_a_fragment_left_in_changelog_d_is_refused(tmp_path: Path) -> None:
    work = _scratch(tmp_path, {"changelog.d/README.md": "r\n", "changelog.d/left-over.md": "x\n"})
    proc = _run(work)
    assert proc.returncode != 0
    assert "changelog.d" in proc.stderr and "left-over.md" in proc.stderr


def test_a_non_markdown_leftover_is_refused_too(tmp_path: Path) -> None:
    work = _scratch(tmp_path, {"changelog.d/README.md": "r\n", "changelog.d/notes.txt": "x\n"})
    proc = _run(work)
    assert proc.returncode != 0 and "notes.txt" in proc.stderr


@pytest.mark.parametrize("files", [{"changelog.d/README.md": "r\n"}, {}])
def test_only_the_readme_passes_the_fragment_check(tmp_path: Path, files: dict[str, str]) -> None:
    proc = _run(_scratch(tmp_path, files))
    assert "changelog.d" not in proc.stderr, proc.stderr

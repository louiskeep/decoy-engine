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


# REG-RELEASE E2 (AT-4): after the ordinary pin check the real script asks the pin CLI whether the
# registry is release-ready. `uv` is a stub that builds a "venv" whose python runs the engine copy,
# so the pin steps run for real without a lock sync.
import re  # noqa: E402
import sys  # noqa: E402

from test_stamp_release_compile import (  # noqa: E402
    engine_clone,
    make_seed,
    two_identities,
)
from tests.unit.test_reason_release_pin import (  # noqa: E402
    add_pending_deprecation,
    add_pending_identity,
    add_pending_summary,
)

STUB_TEST = "def test_stub():\n    pass\n"


@pytest.fixture(scope="module")
def engine_seed(tmp_path_factory) -> Path:
    return make_seed(tmp_path_factory)


def _stub_uv(bin_dir: Path, work: Path) -> Path:
    bin_dir.mkdir()
    calls = bin_dir / "calls.log"
    python = f'#!/bin/sh\\nPYTHONPATH="{work}/src" PYTHONDONTWRITEBYTECODE=1 exec "{sys.executable}" "$@"\\n'
    (bin_dir / "uv").write_text(
        "#!/bin/sh\n"
        f'echo "$1" >> "{calls}"\n'
        'if [ "$1" = sync ]; then\n'
        '  mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
        f"  printf '{python}' > \"$UV_PROJECT_ENVIRONMENT/bin/python\"\n"
        '  chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"\n'
        "fi\n"
        "exit 0\n"
    )
    (bin_dir / "uv").chmod(0o755)
    return calls


def _release_checks(work: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    for rel in ("tests/unit/test_version_agreement.py", "tests/unit/test_reason_release_pin.py"):
        (work / rel).write_text(STUB_TEST)
    (work / "scripts/dev/lock_version.sh").write_text("echo 3.11\n")
    version = re.search(r'(?m)^version = "([^"]+)"$', (work / "pyproject.toml").read_text())[1]
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(work.parent)}
    return subprocess.run(  # noqa: S603
        [BASH, str(work / "scripts/release/release_checks.sh"), version],
        cwd=work, env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip


PIN_STEPS_PASSED = "== DP certified profile"


@pytest.mark.parametrize(
    "add", [add_pending_identity, add_pending_deprecation, add_pending_summary]
)
def test_at4_release_checks_refuses_a_pending_registry(engine_seed, tmp_path: Path, add) -> None:
    r = engine_clone(engine_seed, tmp_path)
    add(r.path)
    _stub_uv(tmp_path / "bin", r.path)
    proc = _release_checks(r.path, tmp_path / "bin")
    assert proc.returncode != 0
    assert "reason release pin current" in proc.stdout + proc.stderr, proc.stderr[-800:]
    assert "release-ready" in proc.stderr and PIN_STEPS_PASSED not in proc.stdout, proc.stderr[
        -800:
    ]


def test_at1b_release_checks_refuses_marked_edits_until_they_are_stamped(
    engine_seed, tmp_path: Path
) -> None:
    r = engine_clone(engine_seed, tmp_path)
    add_pending_deprecation(r.path)
    add_pending_summary(r.path)
    r.commit({}, "fix: deprecate one reason and correct a summary")
    bin_dir = tmp_path / "bin"
    _stub_uv(bin_dir, r.path)
    proc = _release_checks(r.path, bin_dir)
    assert proc.returncode != 0 and PIN_STEPS_PASSED not in proc.stdout, proc.stderr[-800:]
    from scripts.release import stamp_release as sr

    assert sr.stamp_release(r.path, "0.23.0", "2026-11-12") == "1.23.1"
    r.commit({}, "chore: release prep")
    proc = _release_checks(r.path, bin_dir)
    assert PIN_STEPS_PASSED in proc.stdout, proc.stdout[-800:] + proc.stderr[-800:]


def test_at4_release_checks_passes_the_pin_steps_after_an_identity_stamp(
    engine_seed, tmp_path: Path
) -> None:
    r = engine_clone(engine_seed, tmp_path)
    two_identities(r)
    bin_dir = tmp_path / "bin"
    calls = _stub_uv(bin_dir, r.path)
    proc = _release_checks(r.path, bin_dir)
    assert "uncompiled fragments" in proc.stderr and not calls.exists(), (
        "fragments refuse before uv"
    )
    from scripts.release import stamp_release as sr

    assert sr.stamp_release(r.path, "0.23.0", "2026-11-12") == "1.24.0"
    r.commit({}, "chore: release prep")
    proc = _release_checks(r.path, bin_dir)
    assert PIN_STEPS_PASSED in proc.stdout, proc.stdout[-800:] + proc.stderr[-800:]
    assert calls.read_text().split() == ["lock", "sync"]

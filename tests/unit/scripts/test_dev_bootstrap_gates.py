"""The worker harness scripts (`scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh`).

bootstrap must fail loudly when the environment differs from the lock, and
`gates.sh --baseline` must compute which failing tests are branch-only rather
than leave "same failures as baseline" as a claim."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DEV = REPO / "scripts" / "dev"
BASH = shutil.which("bash")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="needs bash and git")


def _run(argv: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    # Point the scripts at the interpreter running this test so nothing is installed.
    env = dict(os.environ, DECOY_VENV=sys.prefix)
    env.pop("VIRTUAL_ENV", None)
    return subprocess.run(  # noqa: S603 -- argv is test literals plus resolved tool paths
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600, check=check
    )


def _git(cwd: Path, *args: str) -> str:
    ident = ["-c", "user.name=gates-test", "-c", "user.email=gates-test@example.invalid"]
    return _run([str(GIT), *ident, *args], cwd, check=True).stdout


@pytest.mark.skipif(shutil.which("uv") is None, reason="bootstrap.sh drives uv")
def test_bootstrap_check_fails_on_a_lock_pin_that_differs_from_the_install(tmp_path: Path) -> None:
    for rel in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "NOTICE"):
        shutil.copy2(REPO / rel, tmp_path / rel)
    shutil.copytree(DEV, tmp_path / "scripts" / "dev")
    lock = tmp_path / "uv.lock"
    text, n = re.subn(
        r'(\nname = "duckdb"\nversion = )"[^"]+"', r'\1"0.0.1"', lock.read_text(), count=1
    )
    assert n == 1, "duckdb entry not found in uv.lock"
    lock.write_text(text)

    proc = _run([str(BASH), "scripts/dev/bootstrap.sh", "--check"], tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert re.search(r"duckdb\s+installed \S+\s+lock 0\.0\.1\s+MISMATCH", out), out
    assert "BOOTSTRAP FAIL: duckdb installed" in out, out
    assert "fix:" in out


def _seeded_clone(tmp_path: Path) -> Path:
    """A clone whose origin/main has one shared failure and whose HEAD adds one more."""
    clone = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", "--shared", str(REPO), str(clone))
    # Test the scripts in this working tree, not whatever the clone's HEAD carries.
    shutil.rmtree(clone / "scripts" / "dev", ignore_errors=True)
    shutil.copytree(DEV, clone / "scripts" / "dev")
    seed = clone / "tests" / "_seeded"
    seed.mkdir()
    (seed / "test_seed_shared.py").write_text("def test_shared():\n    assert False\n")
    (seed / "test_seed_ok.py").write_text("def test_ok():\n    assert True\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "base")
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    (seed / "test_seed_branch.py").write_text("def test_branch():\n    assert False\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "seed")
    return clone


def test_gates_baseline_reports_a_seeded_failure_as_branch_only(tmp_path: Path) -> None:
    clone = _seeded_clone(tmp_path)
    argv = [str(BASH), "scripts/dev/gates.sh", "--steps", "tests", "--baseline", "tests/_seeded"]
    proc = _run(argv, clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "EVIDENCE" in out, out
    ids = out.split("FAILING TEST IDS", 1)[-1]
    assert re.search(r"BRANCH-ONLY\s+tests/_seeded/test_seed_branch\.py::test_branch", ids), out
    assert re.search(r"BOTH\s+tests/_seeded/test_seed_shared\.py::test_shared", ids), out
    assert "test_ok" not in ids, out
    assert "branch-only=1" in ids, out
    worktrees = _git(clone, "worktree", "list").splitlines()
    assert len(worktrees) == 1, f"baseline worktree was not cleaned up: {worktrees}"


def test_gates_passes_a_green_run_with_an_evidence_block(tmp_path: Path) -> None:
    clone = _seeded_clone(tmp_path)
    argv = [str(BASH), "scripts/dev/gates.sh", "--steps", "tests", "tests/_seeded/test_seed_ok.py"]
    proc = _run(argv, clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert re.search(r"(?m)^=+ EVIDENCE =+$", proc.stdout), out
    assert "[tests] PASS passed=1 failed=0" in proc.stdout, out


_BROKEN_REPORTERS = {
    "missing": None,
    "crashing": "raise SystemExit('reporter crashed')\n",
    "silent": "",
    "truncated": "print('=' * 30 + ' EVIDENCE ' + '=' * 30)\n",
    # A complete block is still not a pass when the reporter itself fails.
    "nonzero": "print('=' * 30 + ' EVIDENCE ' + '=' * 30)\nprint('=' * 70)\nraise SystemExit(1)\n",
}


@pytest.mark.parametrize("kind", sorted(_BROKEN_REPORTERS))
def test_gates_fails_when_the_reporter_does_not_print_evidence(tmp_path: Path, kind: str) -> None:
    clone = _seeded_clone(tmp_path)
    reporter = clone / "scripts" / "dev" / "_gates_report.py"
    body = _BROKEN_REPORTERS[kind]
    if body is None:
        reporter.unlink()
    else:
        reporter.write_text(body)
    argv = [str(BASH), "scripts/dev/gates.sh", "--steps", "tests", "tests/_seeded/test_seed_ok.py"]
    proc = _run(argv, clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "gates: FAIL: no complete EVIDENCE block" in proc.stderr, out


@pytest.mark.parametrize("steps", ["tests", ""])
def test_gates_fails_when_no_step_ran(tmp_path: Path, steps: str) -> None:
    # A gate that ran nothing is not a pass, even though nothing failed.
    clone = _seeded_clone(tmp_path)
    argv = [str(BASH), "scripts/dev/gates.sh", "--steps", steps, "tests/_seeded/absent.py"]
    proc = _run(argv, clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    expected = "explicit test path not found" if steps else "gates: FAIL: no step ran"
    assert expected in proc.stderr, out


def _diff_clone(tmp_path: Path, branch_files: dict[str, str]) -> Path:
    """A clone whose origin/main is HEAD~1 and whose HEAD adds `branch_files`."""
    clone = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", "--shared", str(REPO), str(clone))
    shutil.rmtree(clone / "scripts" / "dev", ignore_errors=True)
    shutil.copytree(DEV, clone / "scripts" / "dev")
    _git(clone, "add", "-A")
    _git(clone, "commit", "--allow-empty", "-qm", "base")
    _git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    for rel, body in branch_files.items():
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(body)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "branch")
    return clone


def test_gates_fails_a_code_change_that_selects_no_tests(tmp_path: Path) -> None:
    clone = _diff_clone(tmp_path, {"src/decoy_engine/_x0a_probe.py": "X = 1\n"})
    proc = _run([str(BASH), "scripts/dev/gates.sh", "--steps", "format,tests"], clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "touches code" in proc.stderr and "selects no tests" in proc.stderr, out
    assert "[tests] FAIL" in proc.stdout, out


def test_gates_passes_a_docs_only_change_with_tests_not_applicable(tmp_path: Path) -> None:
    clone = _diff_clone(tmp_path, {"docs/records/x0a-probe.md": "# probe\n"})
    proc = _run([str(BASH), "scripts/dev/gates.sh", "--steps", "format,tests"], clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "not applicable: the diff touches no code paths" in proc.stdout, out


def test_gates_baseline_passes_when_the_only_failure_is_shared_with_main(tmp_path: Path) -> None:
    clone = _seeded_clone(tmp_path)
    argv = [str(BASH), "scripts/dev/gates.sh", "--steps", "format,tests", "--baseline"]
    argv.append("tests/_seeded/test_seed_shared.py")
    proc = _run(argv, clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert re.search(r"BOTH\s+tests/_seeded/test_seed_shared\.py::test_shared", out), out
    assert "(none)" in out.split("BRANCH-ONLY FAILING STEPS", 1)[-1], out


def _report_module():  # type: ignore[no-untyped-def]
    import importlib.util

    spec = importlib.util.spec_from_file_location("_gates_report", DEV / "_gates_report.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_run(out: Path, label: str, rc: int, log: str) -> None:
    with (out / f"{label}.steps").open("a") as fh:
        fh.write(f"mypy\t{rc}\tmypy src\n")
    (out / f"{label}-mypy.log").write_text(log)


def test_branch_only_steps_matches_mypy_errors_across_line_moves(tmp_path: Path) -> None:
    mod = _report_module()
    shared = 'src/a.py:62: error: Unused "type: ignore" comment  [unused-ignore]\n'
    _write_run(tmp_path, "baseline", 1, shared)
    _write_run(tmp_path, "branch", 1, shared.replace(":62:", ":70:"))
    assert mod.branch_only_steps(tmp_path) == []
    extra = tmp_path / "x"
    extra.mkdir()
    _write_run(extra, "baseline", 1, shared)
    _write_run(extra, "branch", 1, shared + "src/b.py:3: error: new problem  [misc]\n")
    assert mod.branch_only_steps(extra) == ["mypy"]


def test_branch_only_steps_flags_a_failure_main_does_not_have(tmp_path: Path) -> None:
    mod = _report_module()
    _write_run(tmp_path, "baseline", 0, "")
    _write_run(tmp_path, "branch", 2, "crashed without parseable lines\n")
    assert mod.branch_only_steps(tmp_path) == ["mypy"]


@pytest.mark.parametrize(
    "probe",
    [
        "scripts/codeset_etl/_x0a_" + "orphan.py",
        "scripts/codeset_etl/_x0a_" + "orphan.sh",
        "Makefile",
    ],
)
def test_gates_fails_a_change_outside_the_old_code_globs_with_no_matching_test(
    tmp_path: Path, probe: str
) -> None:
    # Code is everything not listed as documentation, so a new package cannot slip past the gate.
    clone = _diff_clone(tmp_path, {probe: "X = 1\n"})
    proc = _run([str(BASH), "scripts/dev/gates.sh", "--steps", "format,tests"], clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "touches code" in proc.stderr and "selects no tests" in proc.stderr, out


def test_gates_fails_a_mixed_docs_and_code_change_with_no_tests(tmp_path: Path) -> None:
    files = {
        "docs/records/x0a-mixed.md": "# x\n",
        "scripts/codeset_etl/_x0a_" + "mixed.py": "X = 1\n",
    }
    clone = _diff_clone(tmp_path, files)
    proc = _run([str(BASH), "scripts/dev/gates.sh", "--steps", "format,tests"], clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "selects no tests" in proc.stderr, out


def test_gates_passes_a_change_made_only_of_documentation_paths(tmp_path: Path) -> None:
    files = {"README.md": "# r\n", "docs/a/b.rst": "x\n", ".github/note.yml": "a: 1\n"}
    clone = _diff_clone(tmp_path, files)
    proc = _run([str(BASH), "scripts/dev/gates.sh", "--steps", "format,tests"], clone)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "not applicable: the diff touches no code paths" in proc.stdout, out


_SHARED = 'src/a.py:62: error: Unused "type: ignore" comment  [unused-ignore]\n'


def _verdict(tmp_path: Path, branch: tuple[int, str], base: tuple[int, str]) -> list[str]:
    _write_run(tmp_path, "baseline", *base)
    _write_run(tmp_path, "branch", *branch)
    return _report_module().branch_only_steps(tmp_path)


def test_a_branch_crash_is_branch_only_even_when_main_also_failed(tmp_path: Path) -> None:
    assert _verdict(tmp_path, (139, "Segmentation fault\n"), (1, _SHARED)) == ["mypy"]


@pytest.mark.parametrize("rc", [124, 137, 2])
def test_a_branch_timeout_is_branch_only_even_when_main_timed_out_too(
    tmp_path: Path, rc: int
) -> None:
    assert _verdict(tmp_path, (rc, ""), (rc, "")) == ["mypy"]


def test_a_failure_with_no_identifiable_diagnostic_is_branch_only(tmp_path: Path) -> None:
    assert _verdict(tmp_path, (1, "boom\n"), (1, _SHARED)) == ["mypy"]


def test_a_duplicated_identical_diagnostic_is_branch_only(tmp_path: Path) -> None:
    twice = _SHARED + _SHARED.replace(":62:", ":90:")
    assert _verdict(tmp_path, (1, twice), (1, _SHARED)) == ["mypy"]


def test_identical_failures_on_both_sides_are_shared(tmp_path: Path) -> None:
    assert _verdict(tmp_path, (1, _SHARED.replace(":62:", ":70:")), (1, _SHARED)) == []


def test_a_total_that_disagrees_with_the_parsed_lines_is_branch_only(tmp_path: Path) -> None:
    log = _SHARED + "Found 2 errors in 1 file (checked 3 source files)\n"
    assert _verdict(tmp_path, (1, log), (1, _SHARED)) == ["mypy"]


def test_ruff_concise_diagnostics_are_compared_as_a_multiset(tmp_path: Path) -> None:
    one = "a.py:1:8: F401 [*] `os` imported but unused\nFound 1 error.\n"
    two = one.replace("Found 1", "Found 2") + one.replace("a.py:1:8", "a.py:5:8").splitlines()[0]
    assert _verdict(tmp_path, (1, one.replace(":1:8", ":3:8")), (1, one)) == []
    other = tmp_path / "o"
    other.mkdir()
    assert _verdict(other, (1, two + "\n"), (1, one)) == ["mypy"]


def _write_pytest(out: Path, label: str, ids: list[str]) -> None:
    with (out / f"{label}.steps").open("a") as fh:
        fh.write("tests\t1\tpytest\n")
    (out / f"{label}-tests.xml").write_text("<testsuites/>")
    (out / f"{label}-tests.log").write_text("".join(f"FAILED {i} - boom\n" for i in ids))


def test_a_pytest_failure_main_lacks_is_branch_only_when_main_also_fails(tmp_path: Path) -> None:
    _write_pytest(tmp_path, "baseline", ["tests/test_a.py::test_old"])
    _write_pytest(tmp_path, "branch", ["tests/test_a.py::test_old", "tests/test_b.py::test_new"])
    assert _report_module().branch_only_steps(tmp_path) == ["tests"]
    shared = tmp_path / "s"
    shared.mkdir()
    _write_pytest(shared, "baseline", ["tests/test_a.py::test_old"])
    _write_pytest(shared, "branch", ["tests/test_a.py::test_old"])
    assert _report_module().branch_only_steps(shared) == []

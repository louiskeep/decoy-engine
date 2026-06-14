"""Prove a bugfix's regression test actually catches the bug.

best-practices section 2.4: land the assertion test first and watch it fail on
the pre-fix code. This gate mechanises that for bugfix PRs (opt-in): a test
marked `@pytest.mark.regression` is re-run against the merge-base (the pre-fix
baseline) and MUST fail there. A regression test that already passes without the
fix proves nothing.

How it stays safe: it builds a throwaway git worktree at the merge-base, copies
the PR's new/changed test files onto that old code, and runs the regression
tests there. The working tree you are in is never touched.

Opt-in: CI runs this only on PRs labelled `bugfix` (see the workflow). Run
locally with BASE_REF set (defaults to main).

Exit 0 = the regression test fails (asserts) on the baseline (good).
Exit 1 = no regression test found, it passes on the baseline, the baseline run
was inconclusive (collection/import error), or the baseline could not be
established. All of these mean the bug is not proven.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_MARKER = "@pytest.mark.regression"
_MODULE_MARKER = "regression"  # also catch pytestmark = pytest.mark.regression


def verdict(
    regression_tests_found: bool,
    baseline_pytest_rc: int,
    baseline_had_errors: bool = False,
) -> tuple[bool, str]:
    """Decide pass/fail. Pure, so it is unit-tested.

    Only a clean assertion FAILURE proves the bug was present without the fix.
    pytest exit codes: 0 = all passed, 1 = tests failed, 2 = collection/usage
    error, 5 = no tests collected. A non-1 code, or a run that reported errors
    (an import/fixture/collection error rather than an assertion), is
    inconclusive: it does not prove the test catches the bug.
    """
    if not regression_tests_found:
        return False, (
            "No @pytest.mark.regression test was added/changed in this bugfix PR. "
            "Add the regression test that fails without your fix (best-practices 2.4)."
        )
    if baseline_pytest_rc == 0:
        return False, (
            "The regression test PASSES on the pre-fix baseline. It does not prove "
            "the bug. Make it assert the buggy behaviour so it fails without the fix."
        )
    if baseline_had_errors or baseline_pytest_rc != 1:
        return False, (
            f"The baseline run was inconclusive (pytest exit {baseline_pytest_rc}). "
            "The regression test errored or failed to collect rather than failing a "
            "clean assertion, so it does not prove the bug. It must fail by asserting "
            "the buggy behaviour on the old code, not by erroring on something your "
            "fix adds (a new import, fixture, or helper)."
        )
    return True, "Regression test fails (asserts) on the pre-fix baseline, as required."


def regression_test_files(changed_files: list[str], read_text: Callable[[str], str]) -> list[str]:
    """Changed test files (under tests/, *.py) that carry the regression marker."""
    out = []
    for f in changed_files:
        if not (f.startswith("tests/") and f.endswith(".py")):
            continue
        try:
            text = read_text(f)
        except OSError:
            continue
        if _MARKER in text or f"pytest.mark.{_MODULE_MARKER}" in text:
            out.append(f)
    return out


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def main() -> int:
    base = os.environ.get("BASE_REF", "main")
    merge_base = _git("merge-base", f"origin/{base}", "HEAD")
    changed = [f for f in _git("diff", "--name-only", merge_base, "HEAD").splitlines() if f.strip()]
    reg_files = regression_test_files(changed, lambda f: (REPO / f).read_text(encoding="utf-8"))

    if not reg_files:
        ok, msg = verdict(False, 0)
        print(f"prove-regression FAILED: {msg}", file=sys.stderr)
        return 0 if ok else 1

    tmp = Path(tempfile.mkdtemp(prefix="regression-baseline-"))
    worktree = tmp / "wt"
    try:
        _git("worktree", "add", "--detach", str(worktree), merge_base)
        # Copy the PR's regression test files onto the old code.
        for f in reg_files:
            dst = worktree / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / f, dst)
        env = {**os.environ, "PYTHONPATH": str(worktree / "src")}

        # Guard: the whole proof rests on the baseline's OLD code being imported,
        # not an editable install of HEAD. PYTHONPATH precedence over an editable
        # install is environment-dependent, so confirm decoy_engine actually
        # resolves under the worktree before trusting any result. Refuse loudly
        # otherwise: a gate that silently certifies nothing is worse than none.
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import decoy_engine,sys;sys.stdout.write(decoy_engine.__file__)",
            ],
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
        )
        resolved = probe.stdout.strip()
        if not resolved.startswith(str(worktree / "src")):
            print(
                "prove-regression FAILED: could not establish the pre-fix baseline. "
                f"decoy_engine resolved to {resolved or '<import failed>'!r}, not the "
                "baseline worktree src. Refusing to certify. Run in a clean env "
                "(no editable install of HEAD on PYTHONPATH).",
                file=sys.stderr,
            )
            return 1

        run = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", "-m", _MODULE_MARKER, "-q", "--tb=line", *reg_files],
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
        )
        rc = run.returncode
        # An error/collection failure is not a clean assertion failure: it does
        # not prove the bug (e.g. the test imports something only the fix adds).
        summary = next(
            (ln for ln in reversed((run.stdout + run.stderr).splitlines()) if ln.strip()), ""
        )
        had_errors = "error" in summary.lower()
    finally:
        try:
            _git("worktree", "remove", "--force", str(worktree))
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    ok, msg = verdict(True, rc, baseline_had_errors=had_errors)
    stream = sys.stdout if ok else sys.stderr
    print(f"prove-regression {'OK' if ok else 'FAILED'}: {msg}", file=stream)
    print(f"  (baseline {merge_base[:8]}, tests: {', '.join(reg_files)})", file=stream)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

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

Exit 0 = the regression test fails on the baseline (good) or no bugfix scope.
Exit 1 = no regression test found, or it passes on the baseline (does not prove
the bug).
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


def verdict(regression_tests_found: bool, baseline_pytest_rc: int) -> tuple[bool, str]:
    """Decide pass/fail. Pure, so it is unit-tested.

    rc != 0 on the baseline means the test failed/errored there (the bug is
    present, the fix is absent) which is what we want.
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
    return True, "Regression test fails on the pre-fix baseline, as required."


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
        # Run only the regression tests against the baseline. The worktree's own
        # src goes on PYTHONPATH so the OLD engine code wins over any editable
        # install pointing elsewhere.
        env = {**os.environ, "PYTHONPATH": str(worktree / "src")}
        rc = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", "-m", _MODULE_MARKER, "-q", *reg_files],
            cwd=worktree,
            env=env,
        ).returncode
    finally:
        _git("worktree", "remove", "--force", str(worktree))
        shutil.rmtree(tmp, ignore_errors=True)

    ok, msg = verdict(True, rc)
    stream = sys.stdout if ok else sys.stderr
    print(f"prove-regression {'OK' if ok else 'FAILED'}: {msg}", file=stream)
    print(f"  (baseline {merge_base[:8]}, tests: {', '.join(reg_files)})", file=stream)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

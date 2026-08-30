"""Point the repo's existing mutation harness at one native module, safely.

T0 of the native-efficiency test plan (docs/plans/2026-08-29-native-efficiency-
test-plan.md) needs a standalone-pytest-per-mutant runner for the Python native
package, because mutmut's in-process runner has a documented false-timeout
pathology on Arrow/pandas-substrate code (pyproject.toml [tool.mutmut] and
scripts/tq_mutate.py's module docstring). That runner already exists --
scripts/tq_mutate.py, built for the earlier Test-Quality Program -- so this
script does not reimplement it. It only solves the one gap tq_mutate.py
leaves open: `mutmut run` itself always reads `[tool.mutmut]` from
`./pyproject.toml` with no override flag, and the checked-in block is
permanently pointed at a different module (`_codeset_index.py`). Repointing
it for a one-off native-module run means editing a shared, checked-in file.

This wraps that edit as safely as an in-place edit of a shared file allows:
refuses to run if pyproject.toml already has uncommitted changes (so it can't
silently discard real work), edits only the three keys this needs, runs
`mutmut run` then `tq_mutate.py`, and restores pyproject.toml from git on a
normal exit (`finally`) AND on SIGINT/SIGTERM (a signal handler), the signals
`timeout(1)` and CI time-caps actually send. A hard SIGKILL (kill -9, and what
the Linux OOM-killer sends) cannot be trapped and WILL leave the temporary
block in place; the
next run refuses to start while pyproject.toml is dirty, and the injected block
carries a loud revert-by-hand comment, so a stale edit is caught, not silently
committed or clobbered. Recover a SIGKILL'd run with `git checkout -- pyproject.toml`.

Determinism: the mutmut in-process run is pinned (`PYTHONHASHSEED=0`) so its
kill/survive classification is reproducible. mutmut trusts "killed" as monotonic
(a failing test cannot be un-failed by more tests), but that holds only for
DETERMINISTIC tests: a flaky test can spuriously kill a genuine survivor, and
tq_mutate.py trusts killed verbatim. For the crypto/RI zero-unadjudicated-
survivor bar, pass `--readjudicate-killed` so the killed bucket is ALSO re-run
standalone and any flaky-kill (a "killed" mutant that survives its own fresh
rerun) is surfaced as a real survivor rather than under-counted.

Usage (from the repo root, with the engine venv active or referenced
explicitly):
    python scripts/native-testing/python_mutation_pilot.py \\
        --module src/decoy_engine/execution/native/_kernels_scalar.py \\
        --tests tests/native/test_kernels_scalar.py \\
        --timeout 60

    # Force mutmut's in-process runner to misfire (false timeout), to
    # demonstrate the readjudication path resolves it correctly:
    python scripts/native-testing/python_mutation_pilot.py \\
        --module src/decoy_engine/execution/native/_kernels_scalar.py \\
        --tests tests/native/test_kernels_scalar.py \\
        --force-false-timeout

Leaves `mutants/tq_mutate_report.json` in place afterward (pass
--clean-mutants-dir to remove it) for inspection; pyproject.toml is always
restored.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
MUTANTS_DIR = REPO_ROOT / "mutants"
TQ_MUTATE = REPO_ROOT / "scripts" / "tq_mutate.py"

_SECTION_RE = re.compile(r"(\[tool\.mutmut\]\n)(.*?)(?=\n\[|\Z)", re.DOTALL)


def _venv_python() -> str:
    candidate = REPO_ROOT / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def _git_clean(path: Path) -> bool:
    result = subprocess.run(
        ["git", "diff", "--quiet", "--", str(path.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
    )
    return result.returncode == 0


def _build_mutmut_block(
    *,
    module: str,
    tests: list[str],
    timeout_constant: float | None,
    timeout_multiplier: float | None,
) -> str:
    lines = [
        "[tool.mutmut]",
        "# TEMPORARY: scoped by scripts/native-testing/python_mutation_pilot.py for a",
        "# one-off pilot run. Restored from git when the pilot exits. If you see this",
        "# comment in a committed diff, the restore step did not run -- revert by hand",
        "# with `git checkout -- pyproject.toml`.",
        'source_paths = ["src/decoy_engine"]',
        f"only_mutate = [{module!r}]",
        "pytest_add_cli_args_test_selection = [",
    ]
    lines += [f"    {t!r}," for t in tests]
    lines.append("]")
    if timeout_constant is not None:
        lines.append(f"timeout_constant = {timeout_constant}")
    if timeout_multiplier is not None:
        lines.append(f"timeout_multiplier = {timeout_multiplier}")
    return "\n".join(lines) + "\n"


def _patch_pyproject(new_block: str) -> None:
    text = PYPROJECT.read_text()
    if not _SECTION_RE.search(text):
        raise SystemExit("[tool.mutmut] section not found in pyproject.toml")
    patched = _SECTION_RE.sub(lambda _m: new_block.rstrip("\n"), text, count=1)
    PYPROJECT.write_text(patched)


def _restore_pyproject() -> None:
    subprocess.run(["git", "checkout", "--", "pyproject.toml"], cwd=REPO_ROOT, check=True)


_restoring = False


def _install_restore_signal_handlers() -> None:
    """Restore pyproject.toml on SIGINT/SIGTERM, not just a normal exit.

    `finally` does not run when the process is signalled, and SIGTERM is exactly
    what `timeout(1)` and CI time-caps send -- the realistic way a long crypto/RI
    mutation run ends. Without this, such a kill leaves the shared, checked-in
    config pointed at a throwaway target. SIGKILL cannot be trapped (the Linux
    OOM-killer sends SIGKILL, not SIGTERM); a SIGKILL'd run is caught by the
    next-run dirty-check guard instead, per the module docstring.
    """

    def _handler(signum: int, _frame: object) -> None:
        global _restoring
        if _restoring:  # a second signal mid-restore must not re-enter
            return
        _restoring = True
        _restore_pyproject()
        # Re-raise as the default disposition so the exit status reflects the signal.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _readjudicate_killed_bucket(timeout_s: float) -> int:
    """Re-run every mutant mutmut marked KILLED in its own fresh subprocess, to
    catch a flaky-kill (a genuine survivor a nondeterministic test killed once).

    tq_mutate.py re-adjudicates the survived bucket but trusts killed as monotonic;
    that holds only for deterministic tests. For the crypto/RI zero-survivor bar,
    trusting a flaky kill would under-count survivors -- the dangerous direction.
    Reuses tq_mutate's own readjudicate path; a killed mutant that SURVIVES its
    fresh rerun is reported as a flaky-kill (a real survivor). Returns the flaky
    count so the caller can fail the run.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import tq_mutate  # reused, not reimplemented

    config = tq_mutate.load_mutmut_config(PYPROJECT)
    mutants = tq_mutate.load_mutants(tq_mutate.meta_paths_for(config, MUTANTS_DIR))
    killed = [m for m in mutants if m.mutmut_status == "killed"]
    flaky: list[str] = []
    for m in killed:
        verdict = tq_mutate.readjudicate(m, config, MUTANTS_DIR, timeout_s)
        if verdict.verdict != "killed":
            flaky.append(f"{m.name}: mutmut=killed, standalone={verdict.verdict}")
    print(
        f"\n===== killed-bucket re-adjudication ({len(killed)} killed mutants) =====",
        flush=True,
    )
    if flaky:
        print("FLAKY-KILLS (real survivors under-counted by mutmut's killed):", flush=True)
        for line in flaky:
            print(f"  {line}", flush=True)
    else:
        print("all killed mutants re-confirmed killed standalone (no flaky-kills).", flush=True)
    return len(flaky)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, help="path relative to the repo root")
    parser.add_argument(
        "--tests", nargs="+", required=True, help="pytest_add_cli_args_test_selection entries"
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="tq_mutate.py --timeout floor")
    parser.add_argument(
        "--force-false-timeout",
        action="store_true",
        help="set timeout_constant=timeout_multiplier=0 so mutmut's in-process runner "
        "reports every mutant as a timeout, to demonstrate the readjudication step "
        "resolving it (this repo's own module is too fast to trigger the pathology "
        "spontaneously; this reproduces the same in-process-timeout-vs-fast-standalone "
        "-rerun signature on demand instead of leaving it undemonstrated)",
    )
    parser.add_argument("--clean-mutants-dir", action="store_true")
    parser.add_argument(
        "--readjudicate-killed",
        action="store_true",
        help="also re-run the KILLED bucket standalone and fail on any flaky-kill "
        "(a genuine survivor a nondeterministic test killed once). Required for the "
        "crypto/RI zero-unadjudicated-survivor bar, where under-counting survivors is "
        "the dangerous direction.",
    )
    args = parser.parse_args()

    if not _git_clean(PYPROJECT):
        raise SystemExit(
            "pyproject.toml has uncommitted changes; refusing to scope [tool.mutmut] "
            "over them. Commit or stash first."
        )

    timeout_constant = 0.0 if args.force_false_timeout else None
    timeout_multiplier = 0.0 if args.force_false_timeout else None
    block = _build_mutmut_block(
        module=args.module,
        tests=args.tests,
        timeout_constant=timeout_constant,
        timeout_multiplier=timeout_multiplier,
    )
    # Pin the seed in THIS process's environment before any child spawns, so EVERY
    # adjudication subprocess inherits it: `mutmut run`, the `tq_mutate.py`
    # subprocess (which gets no explicit env), and the killed-bucket re-adjudication
    # (tq_mutate.build_subprocess_env does os.environ.copy()). Seeding only the
    # initial mutmut run would leave the re-adjudication unseeded -- a seed-0
    # survivor could flip to killed on an unseeded rerun and silently vanish from the
    # tally, and the killed-bucket pass only re-checks mutants mutmut marked killed,
    # so it would not catch it. There is no pytest test-order randomizer installed,
    # so the hash seed is the only nondeterminism source to pin.
    os.environ["PYTHONHASHSEED"] = "0"

    # Install the restore handlers BEFORE patching, so a signal arriving during or
    # immediately after the write still restores from git (a checkout of a
    # partially-written file just restores the committed version).
    _install_restore_signal_handlers()
    _patch_pyproject(block)

    try:
        subprocess.run(["rm", "-rf", str(MUTANTS_DIR)], check=True)

        venv_python = _venv_python()
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}

        print("===== mutmut run =====", flush=True)
        run_result = subprocess.run([venv_python, "-m", "mutmut", "run"], cwd=REPO_ROOT, env=env)
        if run_result.returncode != 0:
            print(
                f"`mutmut run` exited {run_result.returncode}; mutant generation itself "
                "failed (this is distinct from having survivors). Aborting before "
                "readjudication.",
                flush=True,
            )
            return 2

        print(
            "\n===== tq_mutate.py (standalone-pytest-per-mutant readjudication) =====", flush=True
        )
        tq_result = subprocess.run(
            [venv_python, str(TQ_MUTATE), "--timeout", str(args.timeout)],
            cwd=REPO_ROOT,
        )
        rc = tq_result.returncode
        if args.readjudicate_killed:
            flaky = _readjudicate_killed_bucket(args.timeout)
            if flaky and rc == 0:
                rc = 1  # under-counted survivors must fail the run
        return rc
    finally:
        _restore_pyproject()
        if args.clean_mutants_dir:
            subprocess.run(["rm", "-rf", str(MUTANTS_DIR)], check=True)
        else:
            print(f"\n(mutants/ left in place; report at {MUTANTS_DIR / 'tq_mutate_report.json'})")


if __name__ == "__main__":
    raise SystemExit(main())

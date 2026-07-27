"""True-verdict mutation post-processor for the Test-Quality Program (TQ).

Why this exists (tq-findings.md #8): mutmut 3.6 grades a module by forking a
child per mutant that runs ``pytest.main()`` IN-PROCESS under a per-mutant
``RLIMIT_CPU`` cap sized from a near-zero per-test duration estimate. On the
engine's heavy execution-substrate suites (pandas/pyarrow) that cap collapses
and MISFIRES: genuinely-surviving mutants get marked timeout instead of
survived, so the reported mutation score is false. Raising ``timeout_constant``
does not fix it -- it is a runner/suite interaction, not a tuning issue.

Key insight that makes this tool simple: mutmut's KILLED verdicts are
trustworthy but its SURVIVED verdicts are not. A kill is MONOTONIC -- if any
test in mutmut's per-mutant selection fails, the mutant is genuinely dead, and
running MORE tests cannot revive it. A "survived", though, only means none of
the tests mutmut CHOSE to run (its coverage-map subset,
``tests_by_mangled_function_name``) killed it; the FULL selection may contain a
killing test mutmut never ran. That is exactly tq-findings.md #16: on a large
fixture-heavy module mutmut's coverage map failed to associate a newly-added
test file, so genuine kills were reported survived and the score came out
false-LOW. So this is a POST-PROCESSOR that RE-ADJUDICATES every
non-trustworthy verdict -- the timeout / suspicious / segfault / no-tests
buckets AND (by default) the survived bucket -- by running each such mutant
against the FULL selection in a fresh ``pytest`` SUBPROCESS with a generous
wall-clock timeout, and corrects the tally. Only mutmut's killed verdicts are
trusted verbatim. ``--trust-survived`` opts the survived bucket back out of
re-adjudication (faster, but sound only when you have confirmed mutmut's
coverage map credits every test in the selection).

The one empirically-nailed part is making the subprocess import the MUTATED
source from ``mutants/`` rather than the editable install. decoy_engine is
installed editable via a plain ``.pth`` that appends ``<repo>/src`` to
sys.path (no PEP 660 meta-path finder), so prepending ``<repo>/mutants/src``
on ``PYTHONPATH`` wins, and running with cwd=``mutants/`` also picks up the
copied test tree -- exactly how mutmut's own in-process runner resolves it
(``execute_pytest`` under ``change_cwd("mutants")``).

Usage:
    uv run --frozen --extra dev --extra mutation \
        python scripts/tq_mutate.py [--run] [--timeout 120] [--jobs 1]

    # then inspect the corrected report
    cat mutants/tq_mutate_report.json

The module + test selection are read from the CURRENT ``[tool.mutmut]`` block
in pyproject.toml (``only_mutate`` + ``pytest_add_cli_args_test_selection``),
never hardcoded.

Correctness posture -- the failure mode this tool must NEVER exhibit is SILENT
UNDER-GRADING (a real survivor or a harness break vanishing from the score while
the tool exits 0). To that end it (P1-3) runs a baseline + forced-fail sanity
check before grading and ABORTS nonzero if the harness is unsound; (P1-1) sizes
the per-mutant wall-clock off the measured baseline and treats a genuine timeout
as UNRESOLVED; (P2-1) re-adjudicates mutmut's "no tests" bucket against the full
selection rather than trusting it; (P2-2) grades the same marker-filtered unit
surface as the repo gate; and (P1-2) exits nonzero with a loud banner if ANY
mutant is left unresolved. A clean exit 0 means every mutant reached a
definitive killed/survived verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

# mutmut's authoritative exit-code -> status map. Imported (not copied) so this
# tool cannot drift from the runner it is correcting. Side-effect-free import.
from mutmut.__main__ import status_by_exit_code

# Statuses mutmut reports that this tool can TRUST verbatim (see module docstring).
# "no tests" is deliberately EXCLUDED (TQ finding P2-1/P2-3): mutmut derives it
# from its per-mutant coverage map, but this tool runs the FULL test selection
# per mutant, so it does not depend on that map. A mutmut-"no tests" mutant,
# re-run against the full selection, correctly resolves to survived (a genuine
# adequacy gap) or killed (proving mutmut's map wrong). After this exclusion,
# an rc-5 "nothing collected" DURING re-adjudication is a HARNESS BREAK, not a
# neutral no-tests -- and it fails the run (see UNRESOLVED_VERDICTS).
#
# "survived" IS in this set (so _trusted_verdict can map it) but is treated as
# trustworthy ONLY under --trust-survived: by default _needs_readjudication
# re-runs the survived bucket against the full selection (tq-findings.md #16),
# because mutmut's survived is not monotonic -- unlike killed, it can be a
# coverage-selection false-survived. The ONLY unconditionally-trusted verdict
# is "killed" (a failing test cannot be un-failed by running more tests).
TRUSTED_STATUSES = frozenset({"killed", "survived", "skipped", "caught by type check"})

# mutmut trusts pytest exit code 3 (internal error) as "killed" (status_by_exit_code
# above). That is the one un-rechecked false-kill vector, so we ALSO re-adjudicate
# any mutant whose mutmut exit code == 3 (TQ finding P3-1). Cheap to re-run.
_MUTMUT_RECHECK_EXIT_CODES = frozenset({3})

# Re-adjudicated verdicts that mean the grade is INCOMPLETE. If any mutant lands
# here the printed score is not trustworthy and main() exits nonzero (P1-2):
#   true-timeout -- hit the wall-clock, verdict genuinely unknown (P1-1)
#   no-tests     -- rc-5 nothing collected during re-adjudication = harness break
#   error        -- rc-4 usage error or any other non-{0,1,2,3,5} rc
UNRESOLVED_VERDICTS = frozenset({"true-timeout", "no-tests", "error"})

# Marker filter mirroring the repo's pyproject addopts (`-m "not benchmark and
# not testflight and not packaging and not codspeed"`). We drop OTHER ini
# addopts with `-o addopts=` but MUST re-add this explicitly (P2-2), otherwise
# the tool grades a different surface (testflight/benchmark) than the unit gate.
_MARKER_FILTER = "not benchmark and not testflight and not packaging and not codspeed"
# The complement, used to assert the selection carries none of those markers.
_EXCLUDED_MARKERS_EXPR = "benchmark or testflight or packaging or codspeed"

# Per-mutant wall-clock timeout multiplier over the measured baseline full-selection
# duration (P1-1). The effective timeout is max(--timeout floor, baseline * MULT).
_BASELINE_TIMEOUT_MULT = 8

# The baseline/marker PROBE runs get their own generous wall-clock, DECOUPLED from
# the per-mutant --timeout floor: the floor is a per-mutant knob and must not be
# able to spuriously abort the harness soundness check (e.g. a deliberately tiny
# floor). Raised automatically if the user sets --timeout above it.
_BASELINE_RUN_TIMEOUT = 600.0

# pytest exit codes: 0 all-passed, 1 tests-failed, 2 interrupted, 3 internal
# error, 4 usage error, 5 no-tests-collected.
_PYTEST_KILLED_CODES = frozenset({1, 2, 3})


@dataclass
class MutmutConfig:
    source_paths: list[str]
    only_mutate: list[str]
    test_selection: list[str]
    extra_cli_args: list[str] = field(default_factory=list)


@dataclass
class Mutant:
    name: str  # full dotted key, e.g. pkg.mod.x__fn__mutmut_3 == MUTANT_UNDER_TEST
    meta_path: Path  # the .meta file this mutant came from
    mutmut_exit_code: int | None
    mutmut_status: str


@dataclass
class Verdict:
    mutant: Mutant
    verdict: str  # survived | killed | true-timeout | no-tests | error
    returncode: int | None
    duration_s: float
    detail: str = ""


@dataclass
class RunResult:
    """One pytest-subprocess run of the full selection under a given mutant key."""

    returncode: int | None  # None iff the wall-clock timeout fired
    duration_s: float
    stdout: str
    stderr: str
    timed_out: bool


def load_mutmut_config(pyproject_path: Path) -> MutmutConfig:
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    cfg = data.get("tool", {}).get("mutmut", {})
    if "only_mutate" not in cfg:
        raise SystemExit(
            f"[tool.mutmut].only_mutate missing from {pyproject_path}; "
            "point it at the module under grade first."
        )
    return MutmutConfig(
        source_paths=list(cfg.get("source_paths", ["src"])),
        only_mutate=list(cfg["only_mutate"]),
        test_selection=list(cfg.get("pytest_add_cli_args_test_selection", [])),
        extra_cli_args=list(cfg.get("pytest_add_cli_args", [])),
    )


def meta_paths_for(config: MutmutConfig, mutants_dir: Path) -> list[Path]:
    """mutmut persists per-file results at mutants/<source-path>.meta."""
    paths = []
    for src in config.only_mutate:
        meta = mutants_dir / (src + ".meta")
        if not meta.exists():
            raise SystemExit(
                f"{meta} not found -- run `mutmut run` first (or pass --run). "
                "The pyproject only_mutate target must match the completed run."
            )
        paths.append(meta)
    return paths


def load_mutants(meta_paths: list[Path]) -> list[Mutant]:
    mutants: list[Mutant] = []
    for meta_path in meta_paths:
        meta = json.loads(meta_path.read_text())
        for name, code in meta["exit_code_by_key"].items():
            mutants.append(
                Mutant(
                    name=name,
                    meta_path=meta_path,
                    mutmut_exit_code=code,
                    mutmut_status=status_by_exit_code[code],
                )
            )
    return mutants


def build_subprocess_env(mutants_dir: Path, mutant_name: str) -> dict[str, str]:
    env = os.environ.copy()
    # Activate this mutant's trampoline branch.
    env["MUTANT_UNDER_TEST"] = mutant_name
    # Prepend the mutated source so it shadows the editable-install `.pth` src.
    mutated_src = str((mutants_dir / "src").resolve())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = mutated_src + (os.pathsep + existing if existing else "")
    # Concurrent runs share cwd=mutants/; don't race on .pyc writes.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _build_pytest_cmd(config: MutmutConfig, *, extra: Sequence[str] = ()) -> list[str]:
    """The re-adjudication pytest invocation. `-o addopts=` drops the repo's ini
    addopts, then `-m _MARKER_FILTER` re-adds JUST the marker exclusion (P2-2) so
    the tool grades the same unit-test surface, not testflight/benchmark tests."""
    return [
        sys.executable,
        "-m",
        "pytest",
        "--rootdir=.",
        "--tb=no",
        "-q",
        "-o",
        "addopts=",  # drop repo addopts (pull markers back in explicitly below)
        "-m",
        _MARKER_FILTER,
        "-p",
        "no:cacheprovider",  # don't write .pytest_cache into shared mutants/
        "-p",
        "no:randomly",
        "-p",
        "no:random-order",
        *extra,
        *config.test_selection,
        *config.extra_cli_args,
    ]


def _run_selection(
    config: MutmutConfig,
    mutants_dir: Path,
    timeout_s: float,
    mutant_name: str,
    *,
    extra: Sequence[str] = (),
) -> RunResult:
    """Run the full test selection once under `mutant_name` (the MUTANT_UNDER_TEST
    value: a full dotted key, "" for the original tree, or "fail" for the forced
    fail probe). Returns a RunResult; returncode is None iff the wall-clock fired.
    """
    cmd = _build_pytest_cmd(config, extra=extra)
    env = build_subprocess_env(mutants_dir, mutant_name)
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603  # cmd is built from vetted pyproject config
            cmd,
            cwd=str(mutants_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(
            returncode=None,
            duration_s=time.monotonic() - start,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            timed_out=True,
        )
    return RunResult(
        returncode=proc.returncode,
        duration_s=time.monotonic() - start,
        stdout=proc.stdout,
        stderr=proc.stderr,
        timed_out=False,
    )


def readjudicate(
    mutant: Mutant, config: MutmutConfig, mutants_dir: Path, timeout_s: float
) -> Verdict:
    """Run one suspect mutant in a fresh pytest subprocess and return the true verdict."""
    res = _run_selection(config, mutants_dir, timeout_s, mutant.name)
    if res.timed_out:
        # UNRESOLVED (P1-1): a real timeout, not a mutmut misfire. Never dropped
        # from the denominator; it fails the run (see UNRESOLVED_VERDICTS).
        return Verdict(
            mutant=mutant,
            verdict="true-timeout",
            returncode=None,
            duration_s=res.duration_s,
            detail=f"wall-clock > {timeout_s:.1f}s",
        )
    rc = res.returncode
    if rc == 0:
        verdict = "survived"
    elif rc == 5:
        verdict = "no-tests"  # nothing collected during re-adjudication = harness break
    elif rc in _PYTEST_KILLED_CODES:
        verdict = "killed"
    else:
        verdict = "error"  # rc 4 usage error or other; surface, don't miscount
    detail = "" if verdict in {"survived", "killed"} else _tail(res.stdout, res.stderr)
    return Verdict(
        mutant=mutant, verdict=verdict, returncode=rc, duration_s=res.duration_s, detail=detail
    )


def _tail(stdout: str, stderr: str, n: int = 400) -> str:
    blob = (stdout or "") + (stderr or "")
    return blob[-n:].strip()


def _snapshot_cwd_files(root: Path) -> set[str]:
    """Relative paths of every file under `root` -- a cheap before/after diff
    to detect tests that write FIXED-NAME files into the shared cwd=mutants/
    (the --jobs>1 race hazard: concurrent workers would clobber each other's
    fixed-name file, flipping a verdict). pytest `tmp_path` writes land in the
    OS temp dir, not here, so a clean tmp_path-based selection leaves this set
    unchanged and parallelism stays safe."""
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def _trusted_verdict(mutant: Mutant) -> str:
    # Map mutmut's trusted status onto our verdict vocabulary.
    return {
        "killed": "killed",
        "survived": "survived",
        "skipped": "skipped",
        "caught by type check": "type-check",
    }[mutant.mutmut_status]


def _needs_readjudication(mutant: Mutant, *, trust_survived: bool) -> bool:
    """A mutant is re-adjudicated if ANY of:
    - its mutmut status is not trusted (timeout / suspicious / segfault /
      no-tests), OR
    - its mutmut exit code is a re-check code (3 == pytest internal error,
      which mutmut trusts as killed -- P3-1), OR
    - it mutmut-"survived" AND survived re-adjudication is on (the default,
      i.e. NOT --trust-survived; tq-findings.md #16). mutmut's survived is not
      monotonic -- it only means mutmut's coverage-selected subset did not kill
      the mutant, so the full selection may still kill it. --trust-survived
      skips this, trusting mutmut's survived verbatim (faster, less sound)."""
    if mutant.mutmut_exit_code in _MUTMUT_RECHECK_EXIT_CODES:
        return True
    if mutant.mutmut_status not in TRUSTED_STATUSES:
        return True
    return not trust_survived and mutant.mutmut_status == "survived"


class BaselineError(RuntimeError):
    """Baseline sanity check failed; the grade must not be emitted (P1-3)."""


def baseline_sanity_check(config: MutmutConfig, mutants_dir: Path, probe_timeout: float) -> float:
    """Prove the harness is sound BEFORE grading (P1-3), mirroring mutmut's
    run_forced_fail: (a) the original tree (MUTANT_UNDER_TEST="") passes, and
    (b) the forced-fail probe (MUTANT_UNDER_TEST="fail") fails. Returns the
    measured full-selection baseline duration (seconds) for timeout sizing.
    Raises BaselineError on any failure -- callers must abort nonzero.
    """
    print("Baseline sanity check (P1-3) ...", flush=True)

    # (a) original tree must pass.
    original = _run_selection(config, mutants_dir, probe_timeout, "")
    if original.timed_out:
        raise BaselineError(
            f"baseline (MUTANT_UNDER_TEST='') did not finish within {probe_timeout:.1f}s; "
            "the selection is broken or hangs.\n" + _tail(original.stdout, original.stderr)
        )
    if original.returncode != 0:
        raise BaselineError(
            f"baseline (MUTANT_UNDER_TEST='') exited {original.returncode}, expected 0 "
            "(original tree must pass before any mutant is graded). "
            "Check test_selection / pyproject [tool.mutmut].\n"
            + _tail(original.stdout, original.stderr)
        )
    baseline_seconds = original.duration_s
    print(f"  (a) original tree passes  ({baseline_seconds:.2f}s)", flush=True)

    # (b) forced-fail probe must fail (the trampoline raises on the literal "fail").
    forced = _run_selection(config, mutants_dir, probe_timeout, "fail")
    if forced.timed_out:
        raise BaselineError(
            f"forced-fail probe (MUTANT_UNDER_TEST='fail') did not finish within "
            f"{probe_timeout:.1f}s (expected a fast failure)."
        )
    if forced.returncode == 0:
        raise BaselineError(
            "forced-fail probe (MUTANT_UNDER_TEST='fail') exited 0, expected nonzero. "
            "The tests never call the mutated functions, so NO mutant could be killed "
            "-- grading would be meaningless.\n" + _tail(forced.stdout, forced.stderr)
        )
    print(f"  (b) forced-fail probe fails (rc={forced.returncode})", flush=True)
    return baseline_seconds


def warn_if_excluded_markers_collected(
    config: MutmutConfig, mutants_dir: Path, probe_timeout: float
) -> int:
    """Warn loudly (P2-2) if the selection collects any test carrying an excluded
    marker (benchmark/testflight/packaging/codspeed). Returns the collected count.
    Runs against the original tree with --collect-only under the COMPLEMENT marker
    expression; rc-5 (nothing collected) is the healthy case.
    """
    res = _run_selection(
        config,
        mutants_dir,
        probe_timeout,
        "",
        extra=("--collect-only", "--override-ini=addopts=", "-m", _EXCLUDED_MARKERS_EXPR),
    )
    # Our _build_pytest_cmd already passes `-m _MARKER_FILTER`; the later `-m` in
    # `extra` wins (pytest uses the last -m), so this run selects EXCLUDED markers.
    if res.timed_out:
        print("  WARNING: excluded-marker probe timed out; could not verify surface.", flush=True)
        return -1
    if res.returncode == 5:
        return 0  # nothing collected -- the selection carries no excluded markers
    if res.returncode == 0:
        tail = _tail(res.stdout, res.stderr, n=600)
        print(
            "\n  !! WARNING (P2-2): the test selection collects tests carrying an "
            "excluded marker\n"
            f"     ({_EXCLUDED_MARKERS_EXPR}). The grade may cover a different surface "
            "than the unit gate.\n"
            f"     collect-only tail:\n{tail}\n",
            flush=True,
        )
        return 1
    # Any other rc: report but don't block (informational probe).
    print(
        f"  note: excluded-marker probe exited {res.returncode} (informational).",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-adjudicate mutmut's non-trustworthy verdicts (timeout / "
        "suspicious / segfault / no-tests, and BY DEFAULT the survived bucket) with "
        "fresh-subprocess pytest runs against the full selection, and correct the tally."
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="pyproject.toml holding [tool.mutmut] (default: ./pyproject.toml)",
    )
    parser.add_argument(
        "--mutants-dir",
        type=Path,
        default=Path("mutants"),
        help="mutmut output tree (default: ./mutants)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="invoke `mutmut run` first (otherwise assume a run already happened)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="per-mutant wall-clock timeout FLOOR (seconds). The effective timeout is "
        f"max(this, measured_baseline * {_BASELINE_TIMEOUT_MULT}) (default floor: 120s)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel re-adjudication subprocesses (default: 1, deterministic)",
    )
    parser.add_argument(
        "--trust-survived",
        action="store_true",
        help="trust mutmut's 'survived' verbatim instead of re-adjudicating it "
        "against the full selection. Faster, but re-introduces the finding #16 "
        "false-LOW risk (a survived that mutmut's coverage map mis-selected). "
        "Default: OFF -- the survived bucket IS re-adjudicated.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="corrected JSON report path (default: <mutants-dir>/tq_mutate_report.json)",
    )
    args = parser.parse_args()

    config = load_mutmut_config(args.pyproject)

    if args.run:
        print("Running `mutmut run` ...", flush=True)
        rc = subprocess.call(["mutmut", "run"])  # noqa: S607  # fixed console-script invocation
        # mutmut exits nonzero when survivors exist; that is expected, not fatal.
        print(f"mutmut run exit code {rc} (nonzero == survivors, expected)")

    mutants_dir = args.mutants_dir
    meta_paths = meta_paths_for(config, mutants_dir)
    mutants = load_mutants(meta_paths)

    # Concurrency guard (dennis MEDIUM): --jobs>1 runs share cwd=mutants/, so a
    # test that writes a FIXED-NAME file into cwd (not via pytest tmp_path) could
    # be raced across workers -> a false kill/survival. Snapshot the tree now;
    # after the single-threaded baseline probes we diff it, and if the selection
    # wrote stray files into cwd we DOWNGRADE to jobs=1 (a correct serial grade
    # beats a possibly-raced parallel one). tmp_path-based selections are unaffected.
    cwd_files_before = _snapshot_cwd_files(mutants_dir)

    # Probe runs (baseline + marker check) use a generous timeout decoupled from
    # the per-mutant floor, so a deliberately tiny --timeout cannot abort them.
    floor_timeout = args.timeout
    probe_timeout = max(floor_timeout, _BASELINE_RUN_TIMEOUT)

    # P1-3: prove the harness is sound before grading anything. Aborts nonzero.
    try:
        baseline_seconds = baseline_sanity_check(config, mutants_dir, probe_timeout)
    except BaselineError as exc:
        print("\n===== BASELINE SANITY CHECK FAILED =====", flush=True)
        print(str(exc), flush=True)
        print("ABORTING: no score emitted (the grading harness is not sound).", flush=True)
        return 2

    # P1-1: size the per-mutant wall-clock off the measured baseline, floored by --timeout.
    per_mutant_timeout = max(floor_timeout, baseline_seconds * _BASELINE_TIMEOUT_MULT)
    print(
        f"  per-mutant timeout = max(floor {floor_timeout:.1f}s, "
        f"baseline {baseline_seconds:.2f}s x {_BASELINE_TIMEOUT_MULT}) "
        f"= {per_mutant_timeout:.1f}s",
        flush=True,
    )

    # P2-2: the selection must not pull in testflight/benchmark/packaging/codspeed tests.
    warn_if_excluded_markers_collected(config, mutants_dir, probe_timeout)

    # Concurrency guard cont'd: the baseline probes just ran the original tree
    # single-threaded. If they left stray fixed-name files in cwd, jobs>1 would
    # race on them -- downgrade to jobs=1 (correct beats fast). Ignore .pyc and
    # this tool's own report artifact.
    jobs = args.jobs
    if jobs > 1:
        stray = {
            f
            for f in (_snapshot_cwd_files(mutants_dir) - cwd_files_before)
            if not f.endswith(".pyc") and "tq_mutate_report" not in f
        }
        if stray:
            sample = ", ".join(sorted(stray)[:5])
            print(
                "\n  !! WARNING (dennis MEDIUM): the baseline run wrote fixed-name "
                f"file(s) into the shared cwd=mutants/ ({len(stray)}: {sample} ...).\n"
                "     Concurrent workers would race on these, so DOWNGRADING to jobs=1 "
                "for a race-free grade. Route the selection's writes through pytest "
                "tmp_path to re-enable parallelism.\n",
                flush=True,
            )
            jobs = 1

    trust_survived = args.trust_survived
    suspect = [m for m in mutants if _needs_readjudication(m, trust_survived=trust_survived)]
    trusted = [m for m in mutants if not _needs_readjudication(m, trust_survived=trust_survived)]

    survived_note = (
        "TRUSTING mutmut survived (--trust-survived; finding #16 risk ACCEPTED)"
        if trust_survived
        else "re-adjudicating survived bucket too (finding #16 fix; default)"
    )
    print(
        f"{len(mutants)} mutants: {len(trusted)} trusted, "
        f"{len(suspect)} suspect (re-adjudicating with timeout={per_mutant_timeout:.1f}s, "
        f"jobs={jobs}); {survived_note}",
        flush=True,
    )

    verdicts: list[Verdict] = [
        Verdict(
            mutant=m,
            verdict=_trusted_verdict(m),
            returncode=m.mutmut_exit_code,
            duration_s=0.0,
            detail="trusted from mutmut",
        )
        for m in trusted
    ]

    def _do(m: Mutant) -> Verdict:
        return readjudicate(m, config, mutants_dir, per_mutant_timeout)

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(_do, m): m for m in suspect}
            for fut in as_completed(futures):
                v = fut.result()
                verdicts.append(v)
                _print_readjudication(v)
    else:
        for m in suspect:
            v = _do(m)
            verdicts.append(v)
            _print_readjudication(v)

    report = build_report(config, mutants, verdicts, trust_survived=trust_survived)
    report_path = args.report or (mutants_dir / "tq_mutate_report.json")
    report_path.write_text(json.dumps(report, indent=2))

    _print_tally(report, report_path)

    # P1-2: refuse to present a clean score if any mutant is unresolved. A clean
    # run (exit 0) means every mutant reached a definitive killed/survived.
    unresolved = [v for v in verdicts if v.verdict in UNRESOLVED_VERDICTS]
    if unresolved:
        _print_unresolved_banner(unresolved)
        return 1
    return 0


def _print_unresolved_banner(unresolved: list[Verdict]) -> None:
    print("\n" + "=" * 44, flush=True)
    print("!!  SCORE NOT TRUSTWORTHY  (P1-2)  !!", flush=True)
    print("=" * 44, flush=True)
    print(
        f"{len(unresolved)} mutant(s) did not reach a definitive killed/survived "
        "verdict.\nThese are true-timeouts or harness breaks, NOT neutral results; "
        "the\ngrade is incomplete. Resolve each before trusting the mutation score:",
        flush=True,
    )
    for v in unresolved:
        print(
            f"  [{v.verdict:<12}] rc={v.returncode} {v.duration_s:5.1f}s  {v.mutant.name}",
            flush=True,
        )
        if v.detail:
            print(f"      {v.detail.splitlines()[-1][:200]}", flush=True)


def _print_readjudication(v: Verdict) -> None:
    was = v.mutant.mutmut_status
    print(
        f"  [{was:>10} -> {v.verdict:<12}] rc={v.returncode} {v.duration_s:5.1f}s  {v.mutant.name}",
        flush=True,
    )


def build_report(
    config: MutmutConfig,
    mutants: list[Mutant],
    verdicts: list[Verdict],
    *,
    trust_survived: bool,
) -> dict:
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v.verdict] = counts.get(v.verdict, 0) + 1

    killed = counts.get("killed", 0)
    survived = counts.get("survived", 0)
    true_timeout = counts.get("true-timeout", 0)
    no_tests = counts.get("no-tests", 0)
    skipped = counts.get("skipped", 0)
    type_check = counts.get("type-check", 0)
    errors = counts.get("error", 0)

    # LOGIC score denominator = definitively-graded, test-covered mutants.
    logic_total = killed + survived
    logic_score = (killed / logic_total) if logic_total else None

    # COVERAGE-INCLUSIVE score (P2-3): also charges the score for every mutant
    # left UNRESOLVED (true-timeout + rc-5 no-tests harness break + error).
    # With "no tests" now re-adjudicated, a genuine adequacy gap already lands in
    # `survived` (inside logic_total), so the two scores converge on a clean run;
    # the inclusive figure still exposes any residual unresolved gap to a reviewer.
    unresolved_total = true_timeout + no_tests + errors
    inclusive_total = logic_total + unresolved_total
    inclusive_score = (killed / inclusive_total) if inclusive_total else None

    mutmut_counts: dict[str, int] = {}
    for m in mutants:
        mutmut_counts[m.mutmut_status] = mutmut_counts.get(m.mutmut_status, 0) + 1

    return {
        "module": config.only_mutate,
        "test_selection": config.test_selection,
        "total_mutants": len(mutants),
        # False (the default) == the survived bucket was re-adjudicated against
        # the full selection (finding #16); True == mutmut's survived trusted.
        "survived_trusted_verbatim": trust_survived,
        "mutmut_raw_counts": mutmut_counts,
        "corrected_counts": counts,
        "corrected": {
            "killed": killed,
            "survived": survived,
            "true_timeout": true_timeout,
            "no_tests": no_tests,
            "skipped": skipped,
            "type_check": type_check,
            "errors": errors,
            "logic_total": logic_total,
            "logic_mutation_score": logic_score,
            "unresolved_total": unresolved_total,
            "inclusive_total": inclusive_total,
            "inclusive_mutation_score": inclusive_score,
        },
        "readjudicated": [
            {
                "mutant": v.mutant.name,
                "mutmut_status": v.mutant.mutmut_status,
                "mutmut_exit_code": v.mutant.mutmut_exit_code,
                "verdict": v.verdict,
                "returncode": v.returncode,
                "duration_s": round(v.duration_s, 2),
                "detail": v.detail,
            }
            for v in verdicts
            if _needs_readjudication(v.mutant, trust_survived=trust_survived)
        ],
    }


def _print_tally(report: dict, report_path: Path) -> None:
    c = report["corrected"]
    print("\n===== CORRECTED MUTATION TALLY =====")
    print(f"module:            {report['module']}")
    print(f"total mutants:     {report['total_mutants']}")
    print(f"mutmut raw:        {report['mutmut_raw_counts']}")
    print("-- corrected --")
    print(f"killed:            {c['killed']}")
    print(f"survived:          {c['survived']}")
    print(f"true-timeout:      {c['true_timeout']}")
    print(f"no-tests:          {c['no_tests']}")
    print(f"skipped:           {c['skipped']}")
    print(f"caught-by-type:    {c['type_check']}")
    if c["errors"]:
        print(f"errors (INSPECT):  {c['errors']}")
    score = c["logic_mutation_score"]
    score_str = f"{score * 100:.2f}%" if score is not None else "n/a"
    print(f"LOGIC score:       {score_str}  ({c['killed']}/{c['logic_total']})")
    inc = c["inclusive_mutation_score"]
    inc_str = f"{inc * 100:.2f}%" if inc is not None else "n/a"
    print(
        f"INCLUSIVE score:   {inc_str}  ({c['killed']}/{c['inclusive_total']})"
        f"  [+{c['unresolved_total']} unresolved]"
    )
    print(f"report written:    {report_path}")


if __name__ == "__main__":
    raise SystemExit(main())

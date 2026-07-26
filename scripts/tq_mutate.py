"""True-verdict mutation post-processor for the Test-Quality Program (TQ).

Why this exists (tq-findings.md #8): mutmut 3.6 grades a module by forking a
child per mutant that runs ``pytest.main()`` IN-PROCESS under a per-mutant
``RLIMIT_CPU`` cap sized from a near-zero per-test duration estimate. On the
engine's heavy execution-substrate suites (pandas/pyarrow) that cap collapses
and MISFIRES: genuinely-surviving mutants get marked timeout instead of
survived, so the reported mutation score is false. Raising ``timeout_constant``
does not fix it -- it is a runner/suite interaction, not a tuning issue.

Key insight that makes this tool simple: mutmut's KILLED verdicts are
trustworthy (a killed mutant genuinely failed a test). Only the timeout /
suspicious / segfault verdicts are suspect. So this is a POST-PROCESSOR: it
reads the verdicts mutmut already persisted, then RE-ADJUDICATES only the
suspect buckets by running each mutant in a fresh ``pytest`` SUBPROCESS with a
generous wall-clock timeout, and corrects the tally.

The one empirically-nailed part is making the subprocess import the MUTATED
source from ``mutants/`` rather than the editable install. decoy_engine is
installed editable via a plain ``.pth`` that appends ``<repo>/src`` to
sys.path (no PEP 660 meta-path finder), so prepending ``<repo>/mutants/src``
on ``PYTHONPATH`` wins, and running with cwd=``mutants/`` also picks up the
copied test tree -- exactly how mutmut's own in-process runner resolves it
(``execute_pytest`` under ``change_cwd("mutants")``).

Usage:
    uv run --frozen --extra dev --extra lint --extra vault \
        python scripts/tq_mutate.py [--run] [--timeout 120] [--jobs 1]

    # then inspect the corrected report
    cat mutants/tq_mutate_report.json

The module + test selection are read from the CURRENT ``[tool.mutmut]`` block
in pyproject.toml (``only_mutate`` + ``pytest_add_cli_args_test_selection``),
never hardcoded.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
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

# Statuses mutmut reports that we TRUST verbatim (see module docstring / #8).
TRUSTED_STATUSES = frozenset({"killed", "survived", "no tests", "skipped", "caught by type check"})

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


def readjudicate(
    mutant: Mutant, config: MutmutConfig, mutants_dir: Path, timeout_s: float
) -> Verdict:
    """Run one suspect mutant in a fresh pytest subprocess and return the true verdict."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--rootdir=.",
        "--tb=no",
        "-q",
        "-o",
        "addopts=",  # drop repo addopts (marker filters etc.); generous run
        "-p",
        "no:cacheprovider",  # don't write .pytest_cache into shared mutants/
        "-p",
        "no:randomly",
        "-p",
        "no:random-order",
        *config.test_selection,
        *config.extra_cli_args,
    ]
    env = build_subprocess_env(mutants_dir, mutant.name)
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
    except subprocess.TimeoutExpired:
        return Verdict(
            mutant=mutant,
            verdict="true-timeout",
            returncode=None,
            duration_s=time.monotonic() - start,
            detail=f"wall-clock > {timeout_s}s",
        )
    dur = time.monotonic() - start
    rc = proc.returncode
    if rc == 0:
        verdict = "survived"
    elif rc == 5:
        verdict = "no-tests"  # nothing collected -- harness/selection problem
    elif rc in _PYTEST_KILLED_CODES:
        verdict = "killed"
    else:
        verdict = "error"  # rc 4 usage error or other; surface, don't miscount
    detail = "" if verdict in {"survived", "killed"} else _tail(proc.stdout, proc.stderr)
    return Verdict(mutant=mutant, verdict=verdict, returncode=rc, duration_s=dur, detail=detail)


def _tail(stdout: str, stderr: str, n: int = 400) -> str:
    blob = (stdout or "") + (stderr or "")
    return blob[-n:].strip()


def _trusted_verdict(mutant: Mutant) -> str:
    # Map mutmut's trusted status onto our verdict vocabulary.
    return {
        "killed": "killed",
        "survived": "survived",
        "no tests": "no-tests",
        "skipped": "skipped",
        "caught by type check": "type-check",
    }[mutant.mutmut_status]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-adjudicate mutmut's suspect (timeout/suspicious/segfault) "
        "verdicts with fresh-subprocess pytest runs and correct the tally."
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
        help="per-mutant wall-clock timeout for re-adjudication (default: 120s)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel re-adjudication subprocesses (default: 1, deterministic)",
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

    suspect = [m for m in mutants if m.mutmut_status not in TRUSTED_STATUSES]
    trusted = [m for m in mutants if m.mutmut_status in TRUSTED_STATUSES]

    print(
        f"{len(mutants)} mutants: {len(trusted)} trusted, "
        f"{len(suspect)} suspect (re-adjudicating with timeout={args.timeout}s, "
        f"jobs={args.jobs})",
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
        return readjudicate(m, config, mutants_dir, args.timeout)

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
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

    report = build_report(config, mutants, verdicts)
    report_path = args.report or (mutants_dir / "tq_mutate_report.json")
    report_path.write_text(json.dumps(report, indent=2))

    _print_tally(report, report_path)
    return 0


def _print_readjudication(v: Verdict) -> None:
    was = v.mutant.mutmut_status
    print(
        f"  [{was:>10} -> {v.verdict:<12}] rc={v.returncode} {v.duration_s:5.1f}s  {v.mutant.name}",
        flush=True,
    )


def build_report(config: MutmutConfig, mutants: list[Mutant], verdicts: list[Verdict]) -> dict:
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

    mutmut_counts: dict[str, int] = {}
    for m in mutants:
        mutmut_counts[m.mutmut_status] = mutmut_counts.get(m.mutmut_status, 0) + 1

    return {
        "module": config.only_mutate,
        "test_selection": config.test_selection,
        "total_mutants": len(mutants),
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
            if v.mutant.mutmut_status not in TRUSTED_STATUSES
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
    print(f"report written:    {report_path}")


if __name__ == "__main__":
    raise SystemExit(main())

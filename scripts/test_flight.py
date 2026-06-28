"""Entry point for the Decoy acceptance test-flight suite.

Runs the multi-job suite that proves engine Phase-5 strategies compose,
post-run distribution is intact, and relationships hold. Intended as a
deliberate pre-merge gate, not a per-commit hook.

Usage:
    python scripts/test_flight.py
    python scripts/test_flight.py --job a_healthcare_claims
    python scripts/test_flight.py --dump-fixtures a_healthcare_claims
    python scripts/test_flight.py --mutate constant_collapse

Exit code:
    0 -- all jobs passed (or Phase 0 scaffold, no jobs run).
    1 -- one or more jobs failed.

Phase 0: scaffold with banner output confirming imports are wired correctly.
Phase 1+: real job execution via _runner.run_suite / _runner.run_job.

Alternative entry for pytest-style sub-selection:
    pytest testflight -m testflight
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `testflight` and `decoy_engine`
# packages are importable when the script is invoked directly (e.g.
# `python scripts/test_flight.py` from the repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

# Confirm the runner package is importable (Phase 0 check).
# Phase 1 replaces this with a real call to run_suite / run_job.
import testflight._runner  # noqa: E402  (path manipulation above must precede)
import testflight._spec  # noqa: E402

_JOBS_DIR = _REPO_ROOT / "testflight" / "jobs"

_PHASE0_BANNER = """\
========================================================================
DECOY TEST-FLIGHT  [Phase 0 scaffold -- no jobs run yet]
========================================================================

Phase 0 scaffold: runner package imported successfully.
  testflight._spec  : OK (JobSpec / InvariantSpec data model)
  testflight._runner: OK (seven-step runner contract stubs)

Discovered jobs:
"""


def _discover_jobs(jobs_dir: Path) -> list[Path]:
    """Return sorted manifest.yaml paths under jobs_dir."""
    return sorted(jobs_dir.glob("*/manifest.yaml"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="test_flight.py",
        description="Decoy acceptance test-flight suite runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Alternative: pytest testflight -m testflight\n"
            "Phase 1+ adds real job execution; Phase 0 is scaffolding only."
        ),
    )
    parser.add_argument(
        "--job",
        metavar="NAME",
        help="Run a single job by name (default: all discovered jobs).",
    )
    parser.add_argument(
        "--dump-fixtures",
        metavar="NAME",
        dest="dump_fixtures",
        help=(
            "Write the generated fixture CSVs for a job to a tmp directory "
            "for human inspection. Mirrors the golden-fixture recovery pattern."
        ),
    )
    parser.add_argument(
        "--mutate",
        metavar="KIND",
        help=(
            "Run a known-bad mutation control. Expects the corresponding "
            "invariant to FAIL (anti-vacuity check). Phase 1+."
        ),
    )
    return parser.parse_args()


def main() -> int:
    """Run the test-flight suite and return an exit code."""
    args = _parse_args()

    manifests = _discover_jobs(_JOBS_DIR)
    print(_PHASE0_BANNER)
    for m in manifests:
        job_name = m.parent.name
        print(f"  {job_name}: {m}")
    if not manifests:
        print("  (none found)")
    print()

    if args.job:
        print(f"Requested job  : {args.job}")
    if args.dump_fixtures:
        print(f"Dump fixtures  : {args.dump_fixtures}")
    if args.mutate:
        print(f"Mutation control: {args.mutate}")

    print(
        "\nPhase 1 implements real job execution via _runner.run_suite().\n"
        "Exit 0 (scaffold complete).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

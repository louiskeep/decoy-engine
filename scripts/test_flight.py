"""Entry point for the Decoy acceptance test-flight suite.

Runs the multi-job suite that proves engine Phase-5 strategies compose,
post-run distribution is intact, and relationships hold. Intended as a
deliberate pre-merge gate, not a per-commit hook.

Usage:
    python scripts/test_flight.py
    python scripts/test_flight.py --job a_healthcare_claims

Exit code:
    0 -- all jobs passed.
    1 -- one or more jobs failed.

Alternative entry for pytest-style sub-selection:
    pytest testflight -m testflight
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `testflight` and `decoy_engine`
# packages are importable when the script is invoked directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from testflight._report import render_report  # noqa: E402
from testflight._runner import JOBS_DIR, discover_jobs, run_job, run_suite  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="test_flight.py",
        description="Decoy acceptance test-flight suite runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Alternative: pytest testflight -m testflight",
    )
    parser.add_argument(
        "--job",
        metavar="NAME",
        help="Run a single job by name (default: all discovered jobs).",
    )
    parser.add_argument(
        "--dump-fixtures",
        action="store_true",
        help=(
            "[DEFERRED] Dump built fixture frames to Parquet in testflight/_artifacts/fixtures/. "
            "Deferred: requires a dry-run path that bypasses pipeline execution; "
            "the fixture builders are already callable directly via fixture.build_X(seed=N)."
        ),
    )
    parser.add_argument(
        "--mutate",
        action="store_true",
        help=(
            "[DEFERRED] Run anti-vacuity mutation controls from test_testflight_teeth.py "
            "as a standalone script step. Deferred: the teeth are already executed by "
            "'pytest testflight -m testflight' and are the canonical gate; a separate "
            "mutation runner would duplicate that gate without adding coverage."
        ),
    )
    return parser.parse_args()


def main() -> int:
    """Run the test-flight suite and return an exit code."""
    args = _parse_args()

    if args.dump_fixtures:
        print(
            "NOTE: --dump-fixtures is deferred. "
            "Fixture frames can be built directly via fixture.build_X(seed=N). "
            "Continuing with normal suite run."
        )
    if args.mutate:
        print(
            "NOTE: --mutate is deferred. "
            "Anti-vacuity teeth run under 'pytest testflight -m testflight'. "
            "Continuing with normal suite run."
        )

    if args.job:
        # Single job mode: no suite-level guard (guard needs all manifests).
        manifests = discover_jobs()
        matching = [m for m in manifests if m.parent.name == args.job]
        if not matching:
            print(f"ERROR: Job '{args.job}' not found. Available jobs:")
            for m in manifests:
                print(f"  {m.parent.name}")
            return 1
        results = [run_job(matching[0])]
    else:
        # Full suite mode (includes suite-level strategy-coverage guard).
        results = run_suite()

    if not results:
        print("No jobs discovered. Add a manifest.yaml under testflight/jobs/.")
        return 0

    report = render_report(results)
    print(report)

    # Write report to testflight/_artifacts/report.md so CI can archive it.
    artifacts_dir = _REPO_ROOT / "testflight" / "_artifacts"
    artifacts_dir.mkdir(exist_ok=True)
    from testflight._report import write_report

    write_report(report, artifacts_dir / "report.md")

    all_passed = all(r.passed for r in results)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

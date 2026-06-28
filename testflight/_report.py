"""Evidence report formatter for the test-flight suite (Phase 2).

Renders InvariantResult lists into the section-10 format defined in the
acceptance test-flight plan. The format is plain-ASCII (no em-dashes, no
Unicode arrows) to satisfy the global no-em-dash rule and the CI check
for ASCII-clean files.

Report format (section 10):
  - Header: job name, topology, timestamp, elapsed.
  - Per-invariant-family result rows (PASS / FAIL) with detail lines.
  - Summary: total invariants, passed, failed; overall PASS/FAIL verdict.

The report is returned as a string and optionally written to a file via
write_report. `python scripts/test_flight.py` calls render_report(run_job(...))
and prints it.
"""

from __future__ import annotations

import datetime
import pathlib

from ._runner import InvariantResult, JobResult


def format_job_section(result: JobResult) -> str:
    """Render the per-job evidence section.

    One line per invariant family with PASS/FAIL and (on failure) the
    first line of the detail message indented below.

    Args:
        result: JobResult from run_job.

    Returns:
        Multi-line plain-ASCII string.
    """
    lines: list[str] = []
    verdict = "PASS" if result.passed else "FAIL"
    lines.append(f"=== Job: {result.job_name} [{verdict}] ===")
    lines.append(f"    elapsed: {result.elapsed_s:.2f}s")

    if result.error:
        lines.append(f"    ERROR: {result.error}")
        return "\n".join(lines)

    for ir in result.invariant_results:
        mark = "[PASS]" if ir.passed else "[FAIL]"
        # Show detail for both PASS (expected-vs-found counts) and FAIL cases.
        if ir.detail:
            lines.append(f"  {mark}  {ir.family}  ({ir.detail.splitlines()[0]})")
            for detail_line in ir.detail.splitlines()[1:5]:
                lines.append(f"           {detail_line}")
        else:
            lines.append(f"  {mark}  {ir.family}")

    return "\n".join(lines)


def format_coverage_section(results: list[JobResult]) -> str:
    """Render the strategy-coverage summary across all jobs.

    Counts distinct invariant families checked and how many passed.

    Args:
        results: List of JobResult from run_suite.

    Returns:
        Multi-line plain-ASCII string.
    """
    all_inv: list[InvariantResult] = []
    for r in results:
        all_inv.extend(r.invariant_results)

    passed = sum(1 for ir in all_inv if ir.passed)
    failed = sum(1 for ir in all_inv if not ir.passed)
    lines = [
        "=== Coverage summary ===",
        f"  Total invariant checks : {len(all_inv)}",
        f"  Passed                 : {passed}",
        f"  Failed                 : {failed}",
    ]
    if failed:
        lines.append("  Failed families:")
        for ir in all_inv:
            if not ir.passed:
                lines.append(f"    - {ir.family}")
    return "\n".join(lines)


def render_report(results: list[JobResult], now: str | None = None) -> str:
    """Render the full section-10 evidence report.

    Args:
        results: List of JobResult objects (one per job).
        now: Optional ISO timestamp for the report header. Defaults to UTC now.

    Returns:
        Full plain-ASCII evidence report string.
    """
    ts = now or datetime.datetime.now(datetime.timezone.utc).isoformat()
    all_passed = all(r.passed for r in results)
    overall = "PASS" if all_passed else "FAIL"

    header = [
        "##############################################",
        "# Decoy Engine Test-Flight Evidence Report   #",
        "##############################################",
        f"# Generated : {ts}",
        f"# Jobs run  : {len(results)}",
        f"# Verdict   : {overall}",
        "##############################################",
        "",
    ]

    job_sections = [format_job_section(r) for r in results]
    coverage = format_coverage_section(results)

    parts = header + job_sections + ["", coverage, ""]
    return "\n".join(parts)


def write_report(report: str, path: pathlib.Path) -> None:
    """Write the evidence report to a file.

    Args:
        report: Rendered report string from render_report.
        path: Output file path.
    """
    path.write_text(report, encoding="utf-8")

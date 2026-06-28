"""Evidence-report formatter for the test-flight suite (Phase 0 stubs).

Renders a deterministic, human-readable evidence report from JobResult
instances. Phase 0 defines the interface; Phase 4 fills in the real
renderer.

Report format (from plan section 10):
  - Deterministic markdown / text, printed to stdout.
  - Written to testflight/_artifacts/report.md.
  - One section per job with per-invariant-family lines.
  - Final PASS / FAIL banner.
  - Every line carries expected-vs-found integers so a deviation is obvious
    without re-reading the raw data.
  - The determinism hash and per-table row deltas make a silent change visible
    even when all booleans pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_ARTIFACTS_DIR = Path(__file__).parent / "_artifacts"


def format_job_section(job_result: Any) -> str:
    """Render the evidence section for one completed job.

    Produces a scannable text block covering determinism, table row counts,
    FK integrity, quality grade, distribution, checksums, Safe Harbor,
    quarantine, sentinels, computed columns, and strategy coverage.

    Args:
        job_result: JobResult instance from _runner.run_job.

    Returns:
        Formatted string section (not yet written to file).

    Raises:
        NotImplementedError: Phase 4 implementation pending.
    """
    raise NotImplementedError("Phase 4: format_job_section")


def format_coverage_section(all_results: list[Any]) -> str:
    """Render the strategy-coverage summary across all jobs.

    Computes the union of strategy keys declared in all manifests and compares
    against the live SCALAR_HANDLERS registry minus the documented allowlist.
    Includes corpora coverage (icd10 / hcpcs / ndc / mcc) and validator coverage.

    Args:
        all_results: List of JobResult instances, one per discovered job.

    Returns:
        Formatted string section with coverage counts.

    Raises:
        NotImplementedError: Phase 4 implementation pending.
    """
    raise NotImplementedError("Phase 4: format_coverage_section")


def render_report(all_results: list[Any], engine_version: str, substrate: str) -> str:
    """Render the full evidence report to a string.

    Assembles the header, one section per job (format_job_section), the
    coverage section, and the final PASS / FAIL banner. Deterministic: given
    the same results and versions, the report string is identical across runs.

    Args:
        all_results: List of JobResult instances.
        engine_version: Engine version string from decoy_engine.__version__.
        substrate: Substrate identifier, e.g. "pandas".

    Returns:
        Full report string (suitable for printing and writing to file).

    Raises:
        NotImplementedError: Phase 4 implementation pending.
    """
    raise NotImplementedError("Phase 4: render_report")


def write_report(report: str, artifacts_dir: Path | None = None) -> Path:
    """Write the rendered report string to the artifacts directory.

    Creates the artifacts directory if it does not exist. Writes
    report.md with UTF-8 encoding.

    Args:
        report: Rendered report string from render_report.
        artifacts_dir: Output directory. Defaults to _ARTIFACTS_DIR.

    Returns:
        Path to the written report.md file.

    Raises:
        NotImplementedError: Phase 4 implementation pending.
    """
    raise NotImplementedError("Phase 4: write_report")

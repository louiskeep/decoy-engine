"""Test-flight runner: 7-step contract (Phase 2 implementation).

Seven-step contract for driving the real engine pipeline and collecting
results:
  1. Load JobSpec from jobs/<job>/manifest.yaml via _spec.load_manifest.
  2. Build deterministic source frames from fixture.py (seeded, plants edge
     cases); fingerprint verified inside each fixture builder.
  3. Assemble the pipeline config dict from the manifest; validate through
     PipelineConfig.model_validate(raw).model_dump() (the real choke-point).
  4. Build a fixed master-key resolver via make_key_resolver(MASTER, LABEL).
  5. Call run_pipeline(config, sources, ...) TWICE with the same seed so the
     determinism invariant can assert value-equal outputs (TH-2.2: this is
     Python value equality via to_pydict()/schema comparison, not a byte
     comparison of serialized bytes).
  6. Evaluate every InvariantSpec via _evaluate.evaluate_invariants.
  7. Aggregate results into an evidence record; _report.py renders it; return
     pass/fail verdict.

Design notes (plan section 3):
  - FIXED_TS pins now_iso for the fidelity report so golden comparisons work.
  - MASTER_KEY is a fixed 32-byte test-only key (NOT a production secret).
  - Sources are written to temp parquet files so profile_source can read them.
    The same frames are also passed as pa.Table objects via sources=... to
    run_pipeline for the masking step.
  - Quarantine output_path is set to a temp JSONL file before each pipeline
    call; the invariant check then verifies file line count.

build_source_frames and assemble_config live in _builder.py (split to keep
both modules within the 600-line limit). They are re-exported from here for
backwards compatibility.

evaluate_invariants and InvariantResult live in _evaluate.py (Phase 5
carry-forward split). Re-exported here so existing callers are unchanged.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from ._builder import assemble_config as assemble_config
from ._builder import build_source_frames as build_source_frames
from ._coverage import check_suite_strategy_coverage
from ._evaluate import InvariantResult as InvariantResult
from ._evaluate import evaluate_invariants as evaluate_invariants
from ._fingerprint import fingerprint_outputs
from ._invariants import FIXED_TS
from ._spec import FlightManifest, load_manifest

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

JOBS_DIR = Path(__file__).parent / "jobs"

# Fixed test-only 32-byte master key. NOT a production secret.
# All test-flight FPE output is deterministic under this key + FIXED_TS.
_MASTER_KEY: bytes = b"testflight-master-key-v1--------"

ENGINE_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class JobResult:
    """Aggregated result of running one test-flight job end-to-end.

    `passed` is True iff every InvariantResult.passed is True.
    `elapsed_s` is the wall-clock seconds for the full job (both pipeline runs
    plus invariant evaluation).
    `fingerprint` is the TH-2.2 / P1-5a cross-process determinism fingerprint
    of result_a's output tables (empty string if the job errored before a
    result was produced). See testflight/_fingerprint.py.
    """

    job_name: str
    passed: bool = False
    elapsed_s: float = 0.0
    invariant_results: list[InvariantResult] = field(default_factory=list)
    error: str | None = None
    fingerprint: str = ""


# ---------------------------------------------------------------------------
# Step 1: manifest loading
# ---------------------------------------------------------------------------


def discover_jobs(jobs_dir: Path | None = None) -> list[Path]:
    """Return sorted manifest.yaml paths found under jobs_dir.

    Discovers all jobs registered in the suite by scanning for manifest.yaml
    files. Sorted for deterministic ordering in the evidence report.

    Args:
        jobs_dir: Root of the jobs directory. Defaults to JOBS_DIR.

    Returns:
        Sorted list of manifest.yaml Path objects.
    """
    root = jobs_dir if jobs_dir is not None else JOBS_DIR
    return sorted(root.glob("*/manifest.yaml"))


def load_job(manifest_path: Path) -> FlightManifest:
    """Step 1: Load and validate a job manifest from its path.

    Thin wrapper around _spec.load_manifest so the runner can be the single
    entry point for callers.

    Args:
        manifest_path: Path to a jobs/<job_name>/manifest.yaml file.

    Returns:
        Validated FlightManifest.

    Raises:
        ValueError: If the manifest fails schema validation.
    """
    return load_manifest(manifest_path)


# ---------------------------------------------------------------------------
# Steps 2-5: fixture building, config assembly, pipeline execution
# ---------------------------------------------------------------------------
# build_source_frames and assemble_config are imported from _builder.py above.


def build_key_resolver(manifest: FlightManifest) -> Any:
    """Step 4: Build a fixed master-key resolver for FPE / keyed strategies.

    Uses make_key_resolver(_MASTER_KEY, manifest.master_key_label) so FPE
    columns produce the same output across runs. _MASTER_KEY is a fixed
    32-byte test-only key.

    Args:
        manifest: Validated FlightManifest (provides master_key_label).

    Returns:
        A derive_key callable accepted by run_pipeline.
    """
    from decoy_engine.context import make_key_resolver

    return make_key_resolver(_MASTER_KEY, manifest.master_key_label)


def run_pipeline_twice(
    config: dict[str, Any],
    sources: dict[str, pa.Table],
    key_resolver: Any,
) -> tuple[Any, Any]:
    """Step 5: Call run_pipeline twice; return (result_a, result_b).

    Both calls use the identical config, sources, seed, key resolver, and
    now_iso timestamp. The determinism invariant (6.1) asserts the two
    results are value-equal (schema + to_pydict()) across all output tables
    and the full quality_metrics block (minus known timing keys) -- not a
    byte comparison of serialized bytes (TH-2.2 doc correction).

    Args:
        config: Validated pipeline config dict.
        sources: dict[table_name, pa.Table] source frames.
        key_resolver: derive_key callable from build_key_resolver.

    Returns:
        Tuple (result_a, result_b) of ExecutionResult instances.
    """
    from decoy_engine.execution._pipeline import run_pipeline

    run_kwargs: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "derive_key": key_resolver,
        "fidelity_report": True,
        "now_iso": FIXED_TS,
    }
    result_a = run_pipeline(config, sources, **run_kwargs)
    result_b = run_pipeline(config, sources, **run_kwargs)
    return result_a, result_b


# ---------------------------------------------------------------------------
# Steps 6-7: invariant evaluation and result aggregation
# ---------------------------------------------------------------------------
# evaluate_invariants is imported from _evaluate.py and re-exported above.


def run_job(manifest_path: Path) -> JobResult:
    """Run one test-flight job end-to-end (all seven steps).

    Orchestrates steps 1-7 from the runner contract. Called by the
    parametrized test in test_testflight.py and by scripts/test_flight.py.

    Args:
        manifest_path: Path to the job's manifest.yaml.

    Returns:
        JobResult with passed verdict, invariant details, and elapsed time.
    """
    t0 = time.monotonic()
    manifest = load_job(manifest_path)
    job_dir = manifest_path.parent

    try:
        # Step 2: build source frames.
        pandas_frames = build_source_frames(manifest, job_dir)

        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)

            # Write sources to parquet for profile_source to load.
            source_paths: dict[str, str] = {}
            pa_sources: dict[str, pa.Table] = {}
            for table_name, df in pandas_frames.items():
                p = td / f"{table_name}_src.parquet"
                df.to_parquet(str(p), index=False)
                source_paths[table_name] = str(p)
                pa_sources[table_name] = pa.Table.from_pandas(df)

            # Step 3: assemble and validate config.
            quarantine_path = str(td / "quarantine.jsonl")
            config = assemble_config(manifest, source_paths, td, quarantine_path)

            # Step 4: build key resolver.
            key_resolver = build_key_resolver(manifest)

            # Step 5: run pipeline twice.
            result_a, result_b = run_pipeline_twice(config, pa_sources, key_resolver)

            # Step 6: evaluate invariants.
            inv_results = evaluate_invariants(manifest, result_a, result_b, pa_sources)

            # TH-2.2 / P1-5a: fingerprint result_a's outputs for the cross-process
            # determinism check (see testflight/_fingerprint.py). Computed from
            # result_a only -- result_b is already proven equal to result_a
            # in-process by check_determinism above when the family runs.
            fingerprint = fingerprint_outputs(result_a.outputs)

    except Exception as exc:
        elapsed = time.monotonic() - t0
        return JobResult(
            job_name=manifest.job_name,
            passed=False,
            elapsed_s=elapsed,
            error=str(exc),
        )

    elapsed = time.monotonic() - t0
    all_passed = all(r.passed for r in inv_results)
    return JobResult(
        job_name=manifest.job_name,
        passed=all_passed,
        elapsed_s=elapsed,
        invariant_results=inv_results,
        fingerprint=fingerprint,
    )


def run_suite(jobs_dir: Path | None = None) -> list[JobResult]:
    """Run all discovered jobs and return their results.

    Discovers manifest.yaml files under jobs_dir, runs each job via run_job,
    and aggregates results.  The suite passes iff every JobResult.passed is
    True, including the suite-level strategy-coverage guard which runs once
    before individual jobs by reading the live SCALAR_HANDLERS registry and
    asserting the union of declared strategies covers it minus the documented
    allowlist in testflight/_coverage.py.

    Args:
        jobs_dir: Root of the jobs directory. Defaults to JOBS_DIR.

    Returns:
        List of JobResult, one per discovered job, preceded by a synthetic
        "[suite-guard]" JobResult carrying the strategy-coverage guard result.
        The guard result is excluded from per-job timing but counts in the
        summary totals and appears in the evidence report.
    """
    manifest_paths = discover_jobs(jobs_dir)
    all_manifests = [load_job(m) for m in manifest_paths]

    # --- Suite-level strategy coverage guard (plan section 6.10) ---
    # Runs once across all manifests.  Reads live SCALAR_HANDLERS registry.
    # A new strategy added without a job or allowlist entry fails here.
    guard_result: InvariantResult
    try:
        coverage_detail = check_suite_strategy_coverage(all_manifests)
        guard_result = InvariantResult(
            family="strategy_coverage_guard",
            passed=True,
            detail=coverage_detail,
        )
    except AssertionError as exc:
        guard_result = InvariantResult(
            family="strategy_coverage_guard",
            passed=False,
            detail=str(exc)[:800],
        )

    suite_guard_job = JobResult(
        job_name="[suite-guard]",
        passed=guard_result.passed,
        elapsed_s=0.0,
        invariant_results=[guard_result],
    )

    job_results = [run_job(m) for m in manifest_paths]
    return [suite_guard_job, *job_results]

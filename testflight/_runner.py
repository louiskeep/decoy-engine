"""Test-flight runner contract (Phase 0 stubs).

Seven-step contract for driving the real engine pipeline and collecting
results. Phase 0 defines the signatures and documented contract; Phase 1
fills in real bodies.

Runner contract steps (mirroring plan section 3):
  1. Load JobSpec from jobs/<job>/manifest.yaml via _spec.load_manifest.
  2. Build deterministic source frames from fixture.py (seeded, plants edge
     cases) or committed CSVs; compute and verify a source fingerprint.
  3. Assemble the pipeline config dict from the manifest; validate through
     PipelineConfig.model_validate(raw).model_dump() (the real choke-point).
  4. Build a fixed master-key resolver via make_key_resolver(MASTER, LABEL).
  5. Call run_pipeline(config, sources, ...) TWICE with the same seed so the
     determinism invariant can assert byte-identical outputs.
  6. Evaluate every InvariantSpec via _invariants.py against ExecutionResult.
  7. Aggregate results into an evidence record; _report.py renders it; return
     pass/fail verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._spec import FlightManifest, load_manifest

# ---------------------------------------------------------------------------
# Result types (Phase 1 fills in real fields)
# ---------------------------------------------------------------------------

JOBS_DIR = Path(__file__).parent / "jobs"


@dataclass
class InvariantResult:
    """Outcome of one invariant-family evaluation for one job.

    `family` matches the InvariantSpec field name (e.g. 'determinism',
    'fk_integrity', 'distribution', ...). `passed` is the verdict.
    `detail` is a human-readable summary for the evidence report.
    """

    family: str
    passed: bool
    detail: str = ""


@dataclass
class JobResult:
    """Aggregated result of running one test-flight job end-to-end.

    `passed` is True iff every InvariantResult.passed is True.
    `elapsed_s` is the wall-clock seconds for the full job (both pipeline runs
    plus invariant evaluation).
    """

    job_name: str
    passed: bool = False
    elapsed_s: float = 0.0
    invariant_results: list[InvariantResult] = field(default_factory=list)
    error: str | None = None


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


def build_source_frames(
    manifest: FlightManifest,
    job_dir: Path,
) -> dict[str, Any]:
    """Step 2: Build deterministic source frames from fixture.py or CSVs.

    Resolves each TableSpec.source_builder reference and calls the fixture
    generator with manifest.seed so output is reproducible under pinned
    faker / numpy versions. Computes a SHA-256 source fingerprint and
    compares it against a committed baseline; raises if the fixtures drifted
    (e.g. after a faker version bump) with a clear "re-baseline deliberately"
    message.

    Returns:
        dict[table_name, pyarrow.Table] for mask tables only (generate tables
        have no source frame; they are driven by the config alone).

    Raises:
        FileNotFoundError: If fixture.py or a committed CSV is missing.
        RuntimeError: If the source fingerprint does not match the baseline.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: build_source_frames")


def assemble_config(manifest: FlightManifest) -> dict[str, Any]:
    """Step 3: Assemble and validate the pipeline config dict.

    Merges manifest.config with the relationships and table declarations,
    then validates through PipelineConfig.model_validate(raw).model_dump()
    to use the real engine choke-point. Any invalid config shape raises
    pydantic.ValidationError before the pipeline is invoked.

    Returns:
        Validated, dumped pipeline config dict suitable for run_pipeline.

    Raises:
        pydantic.ValidationError: If the assembled config is invalid.
        NotImplementedError: Phase 1 implementation pending.
    """
    raise NotImplementedError("Phase 1: assemble_config")


def build_key_resolver(manifest: FlightManifest) -> Any:
    """Step 4: Build a fixed master-key resolver for FPE / keyed strategies.

    Uses make_key_resolver(MASTER, manifest.master_key_label) so FPE columns
    produce the same output across runs. The MASTER key is a fixed test-only
    key stored in the suite (not a real production key).

    Returns:
        A derive_key callable accepted by run_pipeline.

    Raises:
        NotImplementedError: Phase 1 implementation pending.
    """
    raise NotImplementedError("Phase 1: build_key_resolver")


def run_pipeline_twice(
    config: dict[str, Any],
    sources: dict[str, Any],
    key_resolver: Any,
) -> tuple[Any, Any]:
    """Step 5: Call run_pipeline twice; return (result_a, result_b).

    Both calls use the identical config, sources, seed, key resolver, and
    now_iso timestamp. The determinism invariant (6.1) asserts the two
    results are byte-identical across all output tables and the in-pipeline
    quality_metrics fidelity blocks.

    Returns:
        Tuple (result_a, result_b) of ExecutionResult instances.

    Raises:
        NotImplementedError: Phase 1 implementation pending.
    """
    raise NotImplementedError("Phase 1: run_pipeline_twice")


# ---------------------------------------------------------------------------
# Steps 6-7: invariant evaluation and result aggregation
# ---------------------------------------------------------------------------


def evaluate_invariants(
    manifest: FlightManifest,
    result_a: Any,
    result_b: Any,
    sources: dict[str, Any],
) -> list[InvariantResult]:
    """Step 6: Evaluate every InvariantSpec against ExecutionResult.

    Delegates each invariant family to the corresponding function in
    _invariants.py. Returns a flat list of InvariantResult; any assertion
    failure raises AssertionError naming job/table/column/strategy so triage
    localises to one strategy.

    Args:
        manifest: Validated job manifest (carries InvariantSpec).
        result_a: First run ExecutionResult.
        result_b: Second run ExecutionResult (for determinism check).
        sources: dict[table_name, pa.Table] of source frames.

    Returns:
        List of InvariantResult, one per invariant family checked.

    Raises:
        AssertionError: If any invariant fails (naming job/table/column).
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: evaluate_invariants")


def run_job(manifest_path: Path) -> JobResult:
    """Run one test-flight job end-to-end (all seven steps).

    Orchestrates steps 1-7 from the runner contract. Called by the
    parametrized test in test_testflight.py and by scripts/test_flight.py.

    Args:
        manifest_path: Path to the job's manifest.yaml.

    Returns:
        JobResult with passed verdict, invariant details, and elapsed time.

    Raises:
        NotImplementedError: Phase 1 full implementation pending.
    """
    raise NotImplementedError("Phase 1: run_job")


def run_suite(jobs_dir: Path | None = None) -> list[JobResult]:
    """Run all discovered jobs and return their results.

    Discovers manifest.yaml files under jobs_dir, runs each job via run_job,
    and aggregates results. The suite passes iff every JobResult.passed is True.

    Args:
        jobs_dir: Root of the jobs directory. Defaults to JOBS_DIR.

    Returns:
        List of JobResult, one per discovered job.

    Raises:
        NotImplementedError: Phase 1 implementation pending.
    """
    raise NotImplementedError("Phase 1: run_suite")

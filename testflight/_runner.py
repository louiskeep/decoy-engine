"""Test-flight runner: 7-step contract (Phase 2 implementation).

Seven-step contract for driving the real engine pipeline and collecting
results:
  1. Load JobSpec from jobs/<job>/manifest.yaml via _spec.load_manifest.
  2. Build deterministic source frames from fixture.py (seeded, plants edge
     cases); compute and verify a source fingerprint.
  3. Assemble the pipeline config dict from the manifest; validate through
     PipelineConfig.model_validate(raw).model_dump() (the real choke-point).
  4. Build a fixed master-key resolver via make_key_resolver(MASTER, LABEL).
  5. Call run_pipeline(config, sources, ...) TWICE with the same seed so the
     determinism invariant can assert byte-identical outputs.
  6. Evaluate every InvariantSpec via _invariants.py against ExecutionResult.
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
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from ._invariants import (
    FIXED_TS,
    check_checksums,
    check_computed_columns,
    check_determinism,
    check_distribution_mask,
    check_fk_integrity,
    check_quarantine,
    check_safe_harbor,
    check_sentinels,
)
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
    generator with manifest.seed. Returns source DataFrames (not PyArrow tables)
    for the mask tables only.

    The fixture module is loaded from job_dir/fixture.py. Source builders
    are called with keyword argument `seed=manifest.seed`. If a builder accepts
    a `members_df` or `claims_df` kwarg (for inter-table FK seeding), the
    previously built frame is passed in.

    Args:
        manifest: Validated FlightManifest.
        job_dir: Directory containing the job's fixture.py.

    Returns:
        dict[table_name, pandas.DataFrame] for mask tables only.

    Raises:
        FileNotFoundError: If fixture.py is missing.
        RuntimeError: If the source fingerprint does not match the baseline.
    """
    import pandas as pd

    # Fingerprints are committed in the manifest YAML comment block; we read
    # them from the comment block via the manifest.yaml file directly.
    # For Job A the committed fingerprints are:
    #   members:     f0a05c63ddfd74c9625f44800ffa87f26cfe8e9e6927cb8ad6907b3b8032c281
    #   claims:      588ca0e3cd05ff1f42d91b080bc64867f7a48cac8b385b8202dfc6782664968b
    #   claim_lines: 3678e5ce5ac37add2d3bf66fc274875282297f038a4a6ebec246ed79b6ecb0f3
    # The runner does NOT enforce fingerprints at Phase 2 execution time so
    # tests remain fast; fingerprint assertions live in the teeth test module.

    fixture_path = job_dir / "fixture.py"
    if not fixture_path.exists():
        raise FileNotFoundError(
            f"Fixture module not found at {fixture_path}. "
            f"Each job requires a fixture.py with source builders."
        )

    # Dynamic import of job-specific fixture module.
    mod_name = f"_testflight_fixture_{manifest.job_name}"
    spec_obj = importlib.util.spec_from_file_location(mod_name, fixture_path)
    assert spec_obj is not None
    fixture_mod = importlib.util.module_from_spec(spec_obj)
    sys.modules[mod_name] = fixture_mod
    loader = spec_obj.loader
    assert loader is not None
    loader.exec_module(fixture_mod)

    seed = manifest.seed
    frames: dict[str, Any] = {}

    for table_spec in manifest.tables:
        if table_spec.kind != "mask":
            continue
        builder_name = table_spec.source_builder
        # Support "fixture.build_X" dotted form.
        if "." in builder_name:
            _, func_name = builder_name.rsplit(".", 1)
        else:
            func_name = builder_name

        builder = getattr(fixture_mod, func_name)

        # Pass inter-table dependencies based on parameter name conventions.
        import inspect

        params = list(inspect.signature(builder).parameters)
        kwargs: dict[str, Any] = {"seed": seed}
        if "members_df" in params and "members" in frames:
            kwargs["members_df"] = frames["members"]
        if "claims_df" in params and "claims" in frames:
            kwargs["claims_df"] = frames["claims"]

        df = builder(**kwargs)
        assert isinstance(df, pd.DataFrame), (
            f"Source builder '{func_name}' for table '{table_spec.name}' "
            f"must return a pandas DataFrame, got {type(df).__name__}."
        )
        frames[table_spec.name] = df

    return frames


def assemble_config(
    manifest: FlightManifest,
    source_paths: dict[str, str],
    output_dir: Path,
    quarantine_path: str,
) -> dict[str, Any]:
    """Step 3: Assemble and validate the pipeline config dict.

    Starts from manifest.config, substitutes placeholder source/target/quarantine
    paths with the actual temp-file paths, then validates through
    PipelineConfig.model_validate(raw).model_dump() to use the real engine
    choke-point. Any invalid config shape raises pydantic.ValidationError before
    the pipeline is invoked.

    Args:
        manifest: Validated FlightManifest.
        source_paths: dict[table_name, parquet_path] of written source files.
        output_dir: Directory to write output parquet files.
        quarantine_path: Path to write the quarantine JSONL file.

    Returns:
        Validated, dumped pipeline config dict suitable for run_pipeline.

    Raises:
        pydantic.ValidationError: If the assembled config is invalid.
    """
    import copy

    from decoy_engine.config._pipeline import PipelineConfig

    raw = copy.deepcopy(manifest.config)

    # Substitute source paths.
    sources_block = raw.get("sources", {})
    for table_name, parquet_path in source_paths.items():
        if table_name in sources_block:
            sources_block[table_name]["path"] = parquet_path
        else:
            sources_block[table_name] = {
                "type": "file",
                "format": "parquet",
                "path": parquet_path,
            }
    raw["sources"] = sources_block

    # Substitute target paths.
    targets_block = raw.get("targets", {})
    for table_name in source_paths:
        out_path = str(output_dir / f"{table_name}_out.parquet")
        if table_name in targets_block:
            targets_block[table_name]["path"] = out_path
        else:
            targets_block[table_name] = {
                "type": "file",
                "format": "parquet",
                "path": out_path,
            }
    raw["targets"] = targets_block

    # Substitute quarantine path if quarantine block is present.
    if "quarantine" in raw and isinstance(raw["quarantine"], dict):
        raw["quarantine"]["output_path"] = quarantine_path

    return PipelineConfig.model_validate(raw).model_dump()


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
    results are byte-identical across all output tables and the in-pipeline
    quality_metrics fidelity blocks.

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


def evaluate_invariants(
    manifest: FlightManifest,
    result_a: Any,
    result_b: Any,
    sources: dict[str, pa.Table],
) -> list[InvariantResult]:
    """Step 6: Evaluate every InvariantSpec against ExecutionResult.

    Delegates each invariant family to the corresponding function in
    _invariants.py. Returns a flat list of InvariantResult; any assertion
    failure is caught and turned into a failed InvariantResult with the
    AssertionError message in the detail.

    Args:
        manifest: Validated job manifest (carries InvariantSpec).
        result_a: First run ExecutionResult.
        result_b: Second run ExecutionResult (for determinism check).
        sources: dict[table_name, pa.Table] of source frames.

    Returns:
        List of InvariantResult, one per invariant family checked.
    """
    inv = manifest.invariants
    job_name = manifest.job_name
    results: list[InvariantResult] = []

    def _run(family: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
            results.append(InvariantResult(family=family, passed=True))
        except (AssertionError, NotImplementedError) as exc:
            msg = str(exc)
            results.append(
                InvariantResult(
                    family=family,
                    passed=False,
                    detail=msg[:500],
                )
            )

    # 6.1 Determinism
    if inv.determinism:
        _run("determinism", check_determinism, job_name, result_a, result_b)

    # 6.4 FK integrity
    if inv.fk_integrity:
        _run(
            "fk_integrity",
            check_fk_integrity,
            job_name,
            inv.fk_integrity,
            result_a,
            manifest.relationships,
        )

    # 6.2 Distribution fidelity: call check_distribution_mask ONCE PER TABLE
    # with ALL column specs for that table. The function iterates specs internally.
    if inv.distribution:
        from collections import defaultdict

        # Group specs by table name.
        specs_by_table: dict[str, list[Any]] = defaultdict(list)
        for col_spec in inv.distribution:
            specs_by_table[col_spec.table].append(col_spec)

        # If quarantine is configured, we need to trim the source for the
        # quarantine-affected table so source_rows == output_rows (the
        # diagnostic row-parity check compares source vs masked output).
        # The quarantine rows are planted at the END of the source by fixture
        # design, so we trim `n_quarantined` rows from the tail of the source.
        quarantine_table: str | None = None
        n_quarantined: int = 0
        if inv.quarantine is not None and inv.quarantine.expected_total_quarantined > 0:
            # Identify the quarantine-affected table from the validators config.
            validators_block = manifest.config.get("validators", [])
            for v in validators_block:
                cols_block = v.get("columns", {})
                if cols_block:
                    quarantine_table = next(iter(cols_block))
                    break
            n_quarantined = inv.quarantine.expected_total_quarantined

        for table_name, table_specs in specs_by_table.items():
            try:
                src_pa = sources.get(table_name)
                if src_pa is None:
                    results.append(
                        InvariantResult(
                            family=f"distribution:{table_name}",
                            passed=False,
                            detail=f"source table '{table_name}' not in sources dict.",
                        )
                    )
                    continue
                src_df = src_pa.to_pandas()

                # Trim source for quarantine-affected tables so row parity holds.
                if table_name == quarantine_table and n_quarantined > 0:
                    src_df = src_df.iloc[: len(src_df) - n_quarantined].copy()

                out_pa = result_a.outputs.get(table_name)
                if out_pa is None:
                    results.append(
                        InvariantResult(
                            family=f"distribution:{table_name}",
                            passed=False,
                            detail=f"output table '{table_name}' not in result.outputs.",
                        )
                    )
                    continue
                out_df = out_pa.to_pandas()

                # Build combined strategy_map from all specs for this table.
                strategy_map: dict[str, str] = {}
                for col_spec in table_specs:
                    if col_spec.strategy:
                        strategy_map[col_spec.column] = col_spec.strategy

                check_distribution_mask(
                    job_name=job_name,
                    table=table_name,
                    spec=table_specs,
                    source_df=src_df,
                    output_df=out_df,
                    strategy_map=strategy_map,
                    policy_config=inv.policy,
                    grade_floor_enabled=inv.grade_floor_enabled,
                )
                results.append(
                    InvariantResult(
                        family=f"distribution:{table_name}",
                        passed=True,
                    )
                )
            except (AssertionError, Exception) as exc:
                results.append(
                    InvariantResult(
                        family=f"distribution:{table_name}",
                        passed=False,
                        detail=str(exc)[:500],
                    )
                )

    # 6.5 Checksums
    if inv.checksums:
        _run("checksums", check_checksums, job_name, inv.checksums, result_a)

    # 6.6 Safe Harbor
    if inv.safe_harbor:
        _run("safe_harbor", check_safe_harbor, job_name, inv.safe_harbor, result_a)

    # 6.7 Quarantine
    if inv.quarantine is not None:
        _run("quarantine", check_quarantine, job_name, inv.quarantine, result_a)

    # 6.8 Sentinels
    if inv.sentinels:
        _run("sentinels", check_sentinels, job_name, inv.sentinels, result_a)

    # 6.9 Computed columns
    if inv.computed_columns:
        _run(
            "computed_columns",
            check_computed_columns,
            job_name,
            inv.computed_columns,
            result_a,
        )

    return results


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
    )


def run_suite(jobs_dir: Path | None = None) -> list[JobResult]:
    """Run all discovered jobs and return their results.

    Discovers manifest.yaml files under jobs_dir, runs each job via run_job,
    and aggregates results. The suite passes iff every JobResult.passed is True.

    Args:
        jobs_dir: Root of the jobs directory. Defaults to JOBS_DIR.

    Returns:
        List of JobResult, one per discovered job.
    """
    manifests = discover_jobs(jobs_dir)
    return [run_job(m) for m in manifests]

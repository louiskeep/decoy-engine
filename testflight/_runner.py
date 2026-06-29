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

build_source_frames and assemble_config live in _builder.py (split to keep
both modules within the 600-line limit). They are re-exported from here for
backwards compatibility.
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
from ._invariants import (
    FIXED_TS,
    check_chapter_preserve,
    check_checksums,
    check_computed_columns,
    check_correlation_through_masking,
    check_determinism,
    check_distribution_generate,
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
            evidence = fn(*args, **kwargs)
            # Capture optional return-value evidence (expected-vs-found counts)
            # for the report's PASS lines (LOW-2).
            detail = str(evidence) if evidence is not None else ""
            results.append(InvariantResult(family=family, passed=True, detail=detail))
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

    # 6.4 FK integrity.
    # Pass source_frames so check_fk_integrity can verify that remap-policy
    # orphans produce output values that differ from their source keys.
    if inv.fk_integrity:
        _run(
            "fk_integrity",
            check_fk_integrity,
            job_name,
            inv.fk_integrity,
            result_a,
            manifest.relationships,
            sources,  # enables remap-masks-orphan check
        )

    # 6.2/6.3 Distribution fidelity.
    # Mask tables: check_distribution_mask (quality-report based).
    # Generate tables: check_distribution_generate (config-derived baseline only;
    # OWNER DECISION Q3: no committed golden snapshots for generate tables).
    if inv.distribution:
        from collections import defaultdict

        # Group specs by table name.
        specs_by_table: dict[str, list[Any]] = defaultdict(list)
        for col_spec in inv.distribution:
            specs_by_table[col_spec.table].append(col_spec)

        # Build a set of generate table names from the manifest so we can route
        # each table to the correct check (mask vs. generate).
        generate_table_names: set[str] = {
            ts.name for ts in manifest.tables if ts.kind == "generate"
        }
        # Build a lookup of config_table dicts for generate tables (needed by
        # check_distribution_generate to read declared weights/params).
        config_tables: dict[str, dict[str, Any]] = {
            t["name"]: t for t in manifest.config.get("tables", []) if isinstance(t, dict)
        }

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
                if table_name in generate_table_names:
                    # 6.3: generate table -- no source frame; check against config.
                    out_pa = result_a.outputs.get(table_name)
                    if out_pa is None:
                        results.append(
                            InvariantResult(
                                family=f"distribution:{table_name}",
                                passed=False,
                                detail=(
                                    f"generate table '{table_name}' not in "
                                    "result.outputs. Pipeline did not produce it."
                                ),
                            )
                        )
                        continue
                    out_df = out_pa.to_pandas()
                    config_table = config_tables.get(table_name, {})
                    check_distribution_generate(
                        job_name=job_name,
                        table=table_name,
                        spec=table_specs,
                        output_df=out_df,
                        config_table=config_table,
                    )
                    results.append(
                        InvariantResult(
                            family=f"distribution:{table_name}",
                            passed=True,
                        )
                    )
                    continue

                # 6.2: mask table -- source frame required.
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

    # 6.7 Quarantine (pass sources so LOW-1 direct row-count assertion runs)
    if inv.quarantine is not None:
        _run("quarantine", check_quarantine, job_name, inv.quarantine, result_a, sources)

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

    # 6.11 chapter_preserve
    if inv.chapter_preserve:
        _run(
            "chapter_preserve",
            check_chapter_preserve,
            job_name,
            inv.chapter_preserve,
            result_a,
            sources,
        )

    # Phase 3c: relabel-invariant masked-correlation checks.
    # Each MaskedCorrelationSpec declares a pair of value-changing-masked columns
    # (fpe, code_set, etc.). Cramers V is computed over contingency COUNTS on
    # both source and output; assert abs(v_out - v_src) <= tol.
    for mc_spec in inv.masked_correlations:
        family_name = f"masked_correlation:{mc_spec.table}:{mc_spec.col_a}:{mc_spec.col_b}"
        try:
            src_pa = sources.get(mc_spec.table)
            out_pa = result_a.outputs.get(mc_spec.table)
            if src_pa is None:
                results.append(
                    InvariantResult(
                        family=family_name,
                        passed=False,
                        detail=f"source table '{mc_spec.table}' not in sources dict.",
                    )
                )
                continue
            if out_pa is None:
                results.append(
                    InvariantResult(
                        family=family_name,
                        passed=False,
                        detail=f"output table '{mc_spec.table}' not in result.outputs.",
                    )
                )
                continue
            evidence = check_correlation_through_masking(
                job_name,
                mc_spec.table,
                mc_spec.col_a,
                mc_spec.col_b,
                src_pa.to_pandas(),
                out_pa.to_pandas(),
                tol=mc_spec.tol,
                min_assoc=mc_spec.min_assoc,
                strategy_a=mc_spec.strategy_a,
                strategy_b=mc_spec.strategy_b,
            )
            results.append(
                InvariantResult(
                    family=family_name,
                    passed=True,
                    detail=(
                        f"v_src={evidence.get('v_src', '?'):.4f} "
                        f"v_out={evidence.get('v_out', '?'):.4f} "
                        f"diff={evidence.get('diff', '?'):.4f}"
                        if evidence.get("diff") is not None
                        else "degenerate (v undefined, skipped)"
                    ),
                )
            )
        except (AssertionError, ValueError, Exception) as exc:
            results.append(
                InvariantResult(
                    family=family_name,
                    passed=False,
                    detail=str(exc)[:500],
                )
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

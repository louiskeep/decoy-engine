"""Source-frame building and config assembly for the test-flight runner.

Split from _runner.py to keep _runner.py within the 600-line limit.
The runner imports and delegates to these functions; external callers
import from _runner (not here) to preserve the existing public API.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

from ._spec import FlightManifest


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

    Fixture fingerprints are enforced at the fixture level (each build_* function
    calls verify_fingerprint before returning) so a faker/numpy version bump
    that shifts the fixture output fails loudly with a re-baseline instruction.

    Args:
        manifest: Validated FlightManifest.
        job_dir: Directory containing the job's fixture.py.

    Returns:
        dict[table_name, pandas.DataFrame] for mask tables only.

    Raises:
        FileNotFoundError: If fixture.py is missing.
        RuntimeError: If the source fingerprint does not match the baseline
            (raised by verify_fingerprint inside the fixture module).
    """
    import inspect

    import pandas as pd

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
        # source_builder is guaranteed non-None for mask tables by the
        # TableSpec model_validator; the assertion narrows the type for mypy.
        assert table_spec.source_builder is not None, (
            f"TableSpec {table_spec.name!r}: kind='mask' but source_builder is None. "
            "The model_validator should have caught this."
        )
        builder_name: str = table_spec.source_builder
        # Support "fixture.build_X" dotted form.
        if "." in builder_name:
            _, func_name = builder_name.rsplit(".", 1)
        else:
            func_name = builder_name

        builder = getattr(fixture_mod, func_name)

        # Pass inter-table dependencies based on parameter name conventions.
        # Supported: members_df, claims_df (Job A); customers_df, products_df (Job B).
        params = list(inspect.signature(builder).parameters)
        kwargs: dict[str, Any] = {"seed": seed}
        if "members_df" in params and "members" in frames:
            kwargs["members_df"] = frames["members"]
        if "claims_df" in params and "claims" in frames:
            kwargs["claims_df"] = frames["claims"]
        if "customers_df" in params and "customers" in frames:
            kwargs["customers_df"] = frames["customers"]
        if "products_df" in params and "products" in frames:
            kwargs["products_df"] = frames["products"]

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

    # Substitute target paths for mask tables (those with a source_path entry).
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

    # Also substitute target paths for generate-kind tables; they have no source
    # but the engine config still needs a real output path to avoid placeholder
    # paths reaching PipelineConfig validation or the write step.
    for table_spec in manifest.tables:
        if table_spec.kind == "generate":
            gen_out_path = str(output_dir / f"{table_spec.name}_out.parquet")
            if table_spec.name in targets_block:
                targets_block[table_spec.name]["path"] = gen_out_path
            else:
                targets_block[table_spec.name] = {
                    "type": "file",
                    "format": "parquet",
                    "path": gen_out_path,
                }

    raw["targets"] = targets_block

    # Substitute quarantine path if quarantine block is present.
    if "quarantine" in raw and isinstance(raw["quarantine"], dict):
        raw["quarantine"]["output_path"] = quarantine_path

    return PipelineConfig.model_validate(raw).model_dump()

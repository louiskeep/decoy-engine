#!/usr/bin/env python3
"""Stage 2b check A1(b), GCS half: schema-valid (platform-only keys stripped)
GCS descriptor, run end to end against a local fake-gcs-server container.

The plan's own text makes GCS (b) conditional: "only if a GCS emulator is
available without new installs; otherwise record it as not run and why."
`fsouza/fake-gcs-server` was already present in `docker images` on this host
(no pull needed), so this ran it rather than skipping.

Mirrors stage2b_cloud_s3_e2e.py's shape:
  1. Start `fsouza/fake-gcs-server` (docker, ephemeral, `-scheme http`).
  2. Upload a small real Parquet fixture to it via a real
     `google.cloud.storage.Client` pointed at the emulator through the
     SDK's own `STORAGE_EMULATOR_HOST` env var (the SDK's standard emulator
     hook; no engine or platform code changed to add one -- `_gcs_client_kwargs`
     forwards no explicit endpoint override, so this is the ONLY way to reach
     the emulator without modifying production code, which the plan forbids).
  3. Build the engine-valid (platform-only keys stripped, including `region`
     for GCS -- see stage2b_cloud_descriptors.py's note) GCS descriptor.
  4. Call the platform's REAL `api.jobs.v2_cloud_staging._stage_gcs_source`
     against the emulator to download the object to a local staging file.
  5. Feed the staged local file into `decoy_engine.execution.run_pipeline`.
  6. Record success/failure, byte parity, and route/backend evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_LOG = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "runs.jsonl"
SCRATCH = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108
CONTAINER_NAME = "audit-fake-gcs-e2e"


_FIXED_HOST_PORT = 14443  # fixed, not random: fake-gcs-server bakes its own
# reported host:port into object metadata (the "externalUrl" gotcha), so the
# client's SUBSEQUENT media-download request goes to whatever `-external-url`
# says, not to STORAGE_EMULATOR_HOST. A random `-p 0:4443` mapping can't be
# passed to `-external-url` before the container exists, so this uses one
# fixed, unprivileged, audit-scoped port instead. Ephemeral container, freed
# in `_stop_fake_gcs` regardless of outcome.


def _start_fake_gcs() -> str:
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)  # noqa: S603,S607
    external_url = f"http://127.0.0.1:{_FIXED_HOST_PORT}"
    subprocess.run(  # noqa: S603,S607
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            f"{_FIXED_HOST_PORT}:4443",
            "--name",
            CONTAINER_NAME,
            "fsouza/fake-gcs-server",
            "-scheme",
            "http",
            "-external-url",
            external_url,
        ],
        check=True,
        capture_output=True,
    )
    time.sleep(2)
    return external_url


def _stop_fake_gcs() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)  # noqa: S603,S607


def main() -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    SCRATCH.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "cell_id": "R028_cloud_gcs_stripped_e2e_fake_gcs_server",
        "description": (
            "engine-valid (platform-only keys stripped) GCS source descriptor, "
            "staged via the platform's real _stage_gcs_source against a local "
            "fsouza/fake-gcs-server container, then masked end to end via "
            "engine-direct run_pipeline"
        ),
        "kind": "stage2b_cloud_e2e",
        "ledger_ids": [],
        "params": {"entry_point": "platform_stage_gcs_source_then_engine_run_pipeline"},
    }

    emulator_host = None
    try:
        emulator_host = _start_fake_gcs()
        result["params"]["fake_gcs_endpoint"] = emulator_host
        os.environ["STORAGE_EMULATOR_HOST"] = emulator_host
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

        n = 5_000
        src_table = pa.table({"h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string())})
        upload_path = SCRATCH / "gcs_e2e_source.parquet"
        pq.write_table(src_table, upload_path)

        import google.cloud.storage as storage

        client = storage.Client()
        bucket_name = "audit-e2e-gcs-bucket"
        object_name = "audit/e2e/object.parquet"
        bucket = client.create_bucket(bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_filename(str(upload_path))

        descriptor = {
            "type": "gcs",
            "format": "parquet",
            "bucket": bucket_name,
            "object": object_name,
            "credentials_ref": None,
        }

        from decoy_engine.config import PipelineConfig

        PipelineConfig.model_validate(
            {
                "version": 1,
                "global_settings": {"seed": 1},
                "sources": {"t": descriptor},
                "targets": {"t": {"type": "file", "format": "parquet", "path": str(SCRATCH / "gcs_e2e_out.parquet")}},
                "tables": [{"name": "t", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns_h"}]}],
            }
        )
        result["schema_validation"] = "passed (engine-valid subset)"

        from api.jobs.v2_cloud_staging import _stage_gcs_source

        staging_dir = SCRATCH / "gcs_e2e_staging"
        staged_path = _stage_gcs_source(descriptor, staging_dir=staging_dir)
        result["staged_path"] = str(staged_path)
        result["staged_bytes"] = staged_path.stat().st_size
        result["source_bytes"] = upload_path.stat().st_size
        result["staged_byte_identical_to_upload"] = (
            staged_path.read_bytes() == upload_path.read_bytes()
        )

        from decoy_engine.execution import run_pipeline
        from decoy_engine.keyprovider import SecretKeyProvider

        staged_table = pq.read_table(staged_path)
        raw_config = {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(staged_path)}},
            "targets": {"t": {"type": "file", "format": "parquet", "path": str(SCRATCH / "gcs_e2e_out.parquet")}},
            "tables": [{"name": "t", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns_h"}]}],
        }
        config = PipelineConfig.model_validate(raw_config).model_dump()
        key_provider = SecretKeyProvider(secret=bytes(range(32)), key_version="v1")

        t0 = time.time()
        run_result = run_pipeline(
            config,
            {"t": staged_table},
            engine_version="stage2b-probe",
            key_provider=key_provider,
            explain_plan=True,
        )
        wall = time.time() - t0

        qm = run_result.quality_metrics
        unified_leaf = qm.get("unified_slice_activation")
        unified_activated = bool(unified_leaf and unified_leaf.get("activated"))
        result["route_evidence"] = {
            "execution": qm.get("execution"),
            "unified_slice_activated": unified_activated,
            "unified_slice_nodes": unified_leaf.get("nodes") if unified_leaf else None,
        }
        out_table = run_result.outputs["t"]
        result["overall_backend"] = "rust_companion" if unified_activated else "pandas"
        result["row_count_in"] = n
        result["row_counts_out"] = {"t": out_table.num_rows}
        result["wall_seconds"] = round(wall, 4)
        result["end_to_end_success"] = out_table.num_rows == n
        result["outcome"] = "success"
    except Exception as exc:  # noqa: BLE001 - the failure itself is the recorded evidence
        result["outcome"] = "failure"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    finally:
        os.environ.pop("STORAGE_EMULATOR_HOST", None)
        _stop_fake_gcs()

    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_LOG, "a") as fh:
        fh.write(json.dumps(result, sort_keys=True, default=str) + "\n")
    print(json.dumps(result, indent=2, default=str), file=sys.stderr)


if __name__ == "__main__":
    main()

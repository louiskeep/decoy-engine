#!/usr/bin/env python3
"""Stage 2b check A1(b), S3 half: schema-valid (platform-only keys stripped)
S3 descriptor, run end to end against an in-process moto S3 server.

Steps, all real code, no mocks past the moto server itself:
  1. Start a `moto.server.ThreadedMotoServer` (in-process, ephemeral port).
  2. Upload a small real Parquet fixture (hash-mask shape) to a bucket on it
     via a real boto3 client pointed at the moto endpoint.
  3. Build the engine-valid S3 descriptor (the same shape
     `stage2b_cloud_descriptors.py` proved validates cleanly once
     `connection_id`/`connection_name` are stripped).
  4. Call the platform's REAL `api.jobs.v2_cloud_staging._stage_s3_source`
     against the moto server to download the object to a local staging file
     (this is the actual function `stage_cloud_sources` calls on the job
     path; not reimplemented here).
  5. Feed the staged local file into `decoy_engine.execution.run_pipeline`
     (engine-direct entry point) to complete the mask job.
  6. Record success/failure, byte/row parity against the pre-upload table,
     and route/backend evidence.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_LOG = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "runs.jsonl"
SCRATCH = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108


def main() -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    SCRATCH.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "audit-test-key")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "audit-test-secret")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    endpoint_url = f"http://{host}:{port}"

    result: dict = {
        "cell_id": "R027_cloud_s3_stripped_e2e_moto",
        "description": (
            "engine-valid (platform-only keys stripped) S3 source descriptor, "
            "staged via the platform's real _stage_s3_source against an "
            "in-process moto S3 server, then masked end to end via engine-direct "
            "run_pipeline"
        ),
        "kind": "stage2b_cloud_e2e",
        "ledger_ids": [],
        "params": {"entry_point": "platform_stage_s3_source_then_engine_run_pipeline", "moto_endpoint": endpoint_url},
    }

    try:
        import boto3

        n = 5_000
        src_table = pa.table({"h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string())})
        upload_path = SCRATCH / "s3_e2e_source.parquet"
        pq.write_table(src_table, upload_path)

        client = boto3.client("s3", endpoint_url=endpoint_url, region_name="us-east-1")
        bucket = "audit-e2e-bucket"
        key = "audit/e2e/object.parquet"
        client.create_bucket(Bucket=bucket)
        client.upload_file(str(upload_path), bucket, key)

        descriptor = {
            "type": "s3",
            "format": "parquet",
            "bucket": bucket,
            "key": key,
            "region": "us-east-1",
            "endpoint_url": endpoint_url,
            "credentials_ref": None,
        }

        # Step: validation passes cleanly (schema-only, no source/target
        # counterpart needed for this half of the proof -- 1(a)'s script
        # already proved the full-config validation behavior).
        from decoy_engine.config import PipelineConfig

        PipelineConfig.model_validate(
            {
                "version": 1,
                "global_settings": {"seed": 1},
                "sources": {"t": descriptor},
                "targets": {"t": {"type": "file", "format": "parquet", "path": str(SCRATCH / "s3_e2e_out.parquet")}},
                "tables": [{"name": "t", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns_h"}]}],
            }
        )
        result["schema_validation"] = "passed (engine-valid subset)"

        # Step: the platform's REAL staging function, against the moto server.
        from api.jobs.v2_cloud_staging import _stage_s3_source

        staging_dir = SCRATCH / "s3_e2e_staging"
        staged_path = _stage_s3_source(descriptor, staging_dir=staging_dir)
        result["staged_path"] = str(staged_path)
        result["staged_bytes"] = staged_path.stat().st_size
        result["source_bytes"] = upload_path.stat().st_size
        result["staged_byte_identical_to_upload"] = (
            staged_path.read_bytes() == upload_path.read_bytes()
        )

        # Step: feed the staged local file into the engine.
        from decoy_engine.execution import run_pipeline
        from decoy_engine.keyprovider import SecretKeyProvider

        staged_table = pq.read_table(staged_path)
        raw_config = {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(staged_path)}},
            "targets": {"t": {"type": "file", "format": "parquet", "path": str(SCRATCH / "s3_e2e_out.parquet")}},
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
        server.stop()

    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_LOG, "a") as fh:
        fh.write(json.dumps(result, sort_keys=True, default=str) + "\n")
    print(json.dumps(result, indent=2, default=str), file=sys.stderr)


if __name__ == "__main__":
    main()

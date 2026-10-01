#!/usr/bin/env python3
"""Stage 2d, task 4 (M3): the FULL production submission entry point, not
just `validate_v2_config` in isolation.

Stage 2b's R025 called `validate_v2_config` directly on a `resolve_binding`
-shaped S3 descriptor and showed it gets rejected before any cloud client is
built. This script goes one level up: it stores the same dirty descriptor in
a stored-job-style config dict and passes it through
`api.jobs.v2_submission.run_v2_pipeline_from_config` itself -- the function a
real job submission actually calls (`api/jobs/runner.py`) -- with
`boto3.client` monkeypatched to raise if constructed, so the "no cloud client
built" proof covers the real entry point, not only its inner validation
helper.

`run_v2_pipeline_from_config` needs a `job` object (only `job.trigger_detail`
is read, and only AFTER validation succeeds) and a `db` session (only used
for `format: fixed_width` layout expansion and, past validation, source
resolution / the orchestrator). Since the dirty descriptor fails at
`validate_v2_config` -- the FIRST call inside the function, before
`job.trigger_detail` or `db` is ever touched -- a minimal stand-in for both is
enough to prove the point; if the record below shows the exception came from
anywhere else, treat `db`/`job` as under-specified for this input, not as a
narrowing of the claim.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_RECORD = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "environment.json"


def _env_record() -> dict[str, Any]:
    if not ENV_RECORD.exists():
        return {}
    return json.loads(ENV_RECORD.read_text())


def _install_no_cloud_client_guard() -> dict:
    import boto3

    guard = {"boto3_client_constructed": False}
    original = boto3.client

    def guarded_client(*args, **kwargs):
        guard["boto3_client_constructed"] = True
        raise AssertionError("boto3.client constructed -- cloud call happened before rejection")

    boto3.client = guarded_client
    guard["_original"] = original
    return guard


def _dirty_s3_source_config(target_path: str) -> dict:
    # Same shape stage 2b's R025 used: engine-valid S3 fields plus the
    # platform's own provenance keys `resolve_binding` adds
    # (`binding_resolve.py:128-151`), which the engine's `S3Source` model
    # rejects (`extra="forbid"`).
    return {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {
            "t": {
                "type": "s3",
                "format": "parquet",
                "bucket": "audit-bucket",
                "key": "audit/source.parquet",
                "region": "us-east-1",
                "connection_id": 42,
                "connection_name": "audit-connection",
            }
        },
        "targets": {"t": {"type": "file", "format": "parquet", "path": target_path}},
        "tables": [
            {"name": "t", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns_h"}]}
        ],
    }


def main() -> None:
    from pathlib import Path

    # `api.jobs.v2_orchestrator` / `v2_full_frame` / `v2_runner` have a
    # deliberate late-in-file circular import (`v2_runner.py`'s own
    # `# noqa: E402` import of `run_v2_pipeline_from_config` from
    # `v2_orchestrator` at the bottom of the file, re-exported for
    # `api.jobs.runner.py`). Importing `v2_runner` FIRST, standalone, lets it
    # finish loading before anything asks `v2_orchestrator` for a name it has
    # not defined yet; importing `v2_orchestrator` or `v2_submission` first in
    # a fresh interpreter raises `ImportError: cannot import name
    # 'run_v2_pipeline_from_config' from partially initialized module`.
    import api.jobs.v2_runner  # noqa: F401
    from api.jobs.v2_submission import run_v2_pipeline_from_config

    scratch = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108
    scratch.mkdir(parents=True, exist_ok=True)

    guard = _install_no_cloud_client_guard()

    submission_config = _dirty_s3_source_config(
        str(scratch / "stage2d_cloud_full_path.out.parquet")
    )
    job = SimpleNamespace(id=999999, trigger_detail="", status="pending")

    status = "unexpected_success"
    error_type = None
    error_message = None
    try:
        run_v2_pipeline_from_config(
            job,
            db=None,
            submission_config=submission_config,
            upload_dir=str(scratch),
            output_dir=str(scratch),
        )
    except Exception as exc:
        status = "rejected"
        error_type = type(exc).__name__
        error_message = str(exc)[:4000]

    record = {
        "cell_id": "R083_cloud_full_path_run_v2_pipeline_from_config",
        "kind": "stage2d_cloud_full_path",
        "description": (
            "resolve_binding-shaped S3 descriptor stored in a stored-job-style "
            "config, passed through run_v2_pipeline_from_config itself (the real "
            "production submission entry point), not only validate_v2_config"
        ),
        "params": {
            "entry_point": "api.jobs.v2_submission.run_v2_pipeline_from_config",
        },
        "outcome": {
            "status": status,
            "error_type": error_type,
            "error_message": error_message,
        },
        "no_cloud_client_proven": not guard["boto3_client_constructed"],
    }
    env_record = _env_record()
    record["commits"] = {
        "engine_commit": env_record.get("engine", {}).get("commit"),
        "platform_commit": env_record.get("platform", {}).get("commit"),
        "cli_commit": env_record.get("cli", {}).get("commit"),
    }
    record["native_companion_status"] = env_record.get("native_companion", {}).get(
        "native_companion_status", {}
    )
    record["native_companion_sha256"] = env_record.get("native_companion", {}).get("module_sha256")
    print(json.dumps(record, default=str))


if __name__ == "__main__":
    main()

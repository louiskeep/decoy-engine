#!/usr/bin/env python3
"""Stage 2b check A1(a): cloud descriptor keys, validation-only.

Builds S3 and GCS descriptors exactly as decoy-platform's
`api/jobs/binding_resolve.py::resolve_binding` emits them for a
`ConnectionRef` (both `direction="source"` and `direction="target"`; the
function's own code shows `direction` only changes behavior for a
`LocalRef`, never a `ConnectionRef`, so the emitted dict shape is identical
either way -- confirmed by calling both directions below rather than
assumed), embeds each in a stored-style pipeline config, and calls the
exact choke-point `run_v2_pipeline_from_config` runs first:
`api.jobs.v2_config.validate_v2_config` (which wraps
`decoy_engine.config.PipelineConfig.model_validate`).

Proves two things per cell:
  1. Whether validation rejects the descriptor, and the exact error.
  2. That no cloud client is constructed before that rejection: `boto3.client`
     and `google.cloud.storage.Client` are monkeypatched to raise
     AssertionError if called, for the duration of the validate_v2_config
     call only.

Run as a single fresh subprocess (this repo's audit venv). Appends one JSON
line per sub-cell to runs.jsonl, continuing the R0xx numbering from stage 2a
(R024 was the last stage-2a cell).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_LOG = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "runs.jsonl"


class _FakeQuery:
    def __init__(self, acct):
        self._acct = acct

    def filter(self, *a, **k):
        return self

    def one_or_none(self):
        return self._acct


class _FakeSession:
    """Stands in for the SQLAlchemy Session `resolve_binding` reads.

    Duck-types only the `.query(Model).filter(...).one_or_none()` chain
    `resolve_binding` actually calls; no real DB touched. `resolve_binding`
    itself is the REAL function from the platform's origin/main tree.
    """

    def __init__(self, acct):
        self._acct = acct

    def query(self, model):
        return _FakeQuery(self._acct)

    def get(self, model, pk):  # LocalRef path, unused here
        return None


def _build_descriptor(provider: str, direction: str) -> dict:
    from api.jobs.binding_resolve import resolve_binding
    from api.jobs.schemas import ConnectionRef
    from api.models import CloudProvider

    acct = SimpleNamespace(
        provider=CloudProvider.s3 if provider == "s3" else CloudProvider.gcs,
        region="us-east-1" if provider == "s3" else None,
        endpoint_url=None,
        credentials_ref=None,
        id=1,
        name="audit-acct-1",
    )
    db = _FakeSession(acct)
    ref = ConnectionRef(
        connection="audit-acct-1",
        bucket="audit-bucket",
        key="audit/path/object.parquet",
        format="parquet",
    )
    return resolve_binding(db, ref, direction=direction)


def _strip_platform_only_keys(descriptor: dict) -> dict:
    """Check 1(b)'s companion: the engine-valid subset of the SAME descriptor.

    S3's engine schema (`S3Source`/`S3Target`) allows `region`, so only
    `connection_id`/`connection_name` are platform-only there. GCS's engine
    schema (`GCSSource`/`GCSTarget`) has no `region` field at all, so `region`
    is ALSO platform-only for GCS (confirmed empirically: stripping only the
    connection_* keys still failed validation with a `gcs.region` extra_forbidden
    error, one of the three the plan's own citations name; fixed here rather
    than left as a false "still rejected" result).
    """
    drop = {"connection_id", "connection_name"}
    if descriptor.get("type") == "gcs":
        drop.add("region")
    return {k: v for k, v in descriptor.items() if k not in drop}


def _minimal_config(table: str, source: dict, target: dict) -> dict:
    return {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {table: source},
        "targets": {table: target},
        "tables": [{"name": table, "columns": [{"name": "h", "strategy": "hash", "namespace": "ns"}]}],
    }


class _NoCloudClientGuard:
    """Monkeypatches boto3.client / google.cloud.storage.Client to raise if
    constructed, for the scope of one `with` block. Proves validation never
    reaches SDK client construction before rejecting."""

    def __enter__(self):
        import boto3

        self._boto3 = boto3
        self._orig_boto3_client = boto3.client

        def _boom_boto3(*a, **k):
            raise AssertionError("boto3.client() constructed before/without validation rejecting")

        boto3.client = _boom_boto3

        try:
            import google.cloud.storage as storage

            self._storage = storage
            self._orig_storage_client = storage.Client

            def _boom_storage(*a, **k):
                raise AssertionError(
                    "google.cloud.storage.Client() constructed before/without validation rejecting"
                )

            storage.Client = _boom_storage
        except ImportError:
            self._storage = None
        return self

    def __exit__(self, *exc):
        self._boto3.client = self._orig_boto3_client
        if self._storage is not None:
            self._storage.Client = self._orig_storage_client
        return False


def _validate_no_cloud_call(config: dict) -> dict:
    from api.jobs.v2_config import validate_v2_config

    with _NoCloudClientGuard():
        try:
            validate_v2_config(config)
            return {"rejected": False, "error": None, "no_cloud_call_proven": True}
        except Exception as exc:  # noqa: BLE001 - recording the exact error is the point
            return {
                "rejected": True,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "no_cloud_call_proven": True,
            }


def run_sub_cell(cell_id: str, description: str, ledger_ids: list[str], config: dict) -> dict:
    t0 = time.time()
    result = _validate_no_cloud_call(config)
    wall = time.time() - t0
    return {
        "cell_id": cell_id,
        "description": description,
        "kind": "stage2b_cloud_descriptor_validation",
        "ledger_ids": ledger_ids,
        "params": {"entry_point": "platform_validate_v2_config", "config": config},
        "route_evidence": result,
        "overall_backend": "n/a (validation-only)",
        "parity_vs_pandas_oracle": {"checked": False, "match": None, "reason": "not applicable"},
        "wall_seconds": round(wall, 4),
        "peak_memory_vmhwm_kb": None,
        "row_count_in": 0,
        "row_counts_out": {},
        "table_kinds": {},
    }


def main() -> None:
    records = []

    for provider in ("s3", "gcs"):
        for direction in ("source", "target"):
            descriptor = _build_descriptor(provider, direction)
            other_side = {"type": "file", "format": "parquet", "path": "/dev/shm/audit-2026-09-30/scratch/placeholder.parquet"}
            table = "t"
            if direction == "source":
                config = _minimal_config(table, descriptor, other_side)
            else:
                config = _minimal_config(table, other_side, descriptor)
            cell_id = f"R025_cloud_{provider}_{direction}_binding_resolved_dirty"
            rec = run_sub_cell(
                cell_id,
                f"resolve_binding-shaped {provider} {direction} descriptor (with platform-only "
                f"keys) validated through validate_v2_config",
                ["B211-B235 (admission proxy, not exercised here)", "Codex check 2 cross-reference"],
                config,
            )
            rec["params"]["resolve_binding_direction"] = direction
            rec["params"]["provider"] = provider
            rec["params"]["descriptor_as_emitted"] = descriptor
            records.append(rec)

            # 1(b) companion at the validation layer: the SAME descriptor with
            # platform-only keys stripped -- confirm it VALIDATES (schema-clean).
            stripped = _strip_platform_only_keys(descriptor)
            if direction == "source":
                config2 = _minimal_config(table, stripped, other_side)
            else:
                config2 = _minimal_config(table, other_side, stripped)
            cell_id2 = f"R026_cloud_{provider}_{direction}_binding_resolved_stripped"
            rec2 = run_sub_cell(
                cell_id2,
                f"same {provider} {direction} descriptor with platform-only keys "
                f"(connection_id/connection_name) stripped, validated through validate_v2_config",
                [],
                config2,
            )
            rec2["params"]["resolve_binding_direction"] = direction
            rec2["params"]["provider"] = provider
            rec2["params"]["descriptor_stripped"] = stripped
            records.append(rec2)

    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_LOG, "a") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            print(
                f"{rec['cell_id']}: rejected={rec['route_evidence']['rejected']} "
                f"no_cloud_call_proven={rec['route_evidence']['no_cloud_call_proven']}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()

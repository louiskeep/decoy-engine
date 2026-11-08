"""Shared builders for the records tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from decoy_engine import records
from decoy_engine.records import payload_digest, record_id

CORPUS = Path(__file__).resolve().parents[2] / "integration" / "compat_corpus" / "records"
PRODUCER = {"name": "decoy-cli", "version": "0.9.0", "engine_version": "0.20.0"}
INVOCATION = "11111111-2222-4333-8444-555555555555"
T0 = datetime(2026, 11, 8, 12, 0, 0, 123456, tzinfo=timezone.utc)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def sample_payload(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sample_size": 50,
        "root_table": "médecins",
        "table_rows": {"médecins": 50, "claims": 212},
        "subset_seed_counts": {"médecins": 50},
        "approximate_columns": [{"table": "claims", "column": "status"}],
        "scratch_dir": ".decoy/scratch/sample-1",
        "outputs": [
            {"table": "claims", "path": "/work/.decoy/scratch/sample-1/claims.csv", "sha256": SHA_A}
        ],
        "ttl_seconds": 86400,
    }
    payload.update(over)
    return payload


def make(kind: str = "sample-run", payload: Any = None, **kw: Any) -> records.RecordEnvelope:
    args: dict[str, Any] = {
        "producer": PRODUCER,
        "invocation_id": INVOCATION,
        "created_at": T0,
        "labels": {"pipeline": "pipelines/claims.yaml", "tables": ["claims"], "status": "ok"},
    }
    args.update(kw)
    return records.build(kind, sample_payload() if payload is None else payload, **args)


def seal(env: dict[str, Any]) -> dict[str, Any]:
    """Recompute digest and id after a test edited an envelope dict."""
    env = dict(env)
    env["payload_digest"] = payload_digest(env["payload"])
    env["id"] = record_id(env)
    return env


def raw_of(env: records.RecordEnvelope) -> dict[str, Any]:
    return json.loads(records.encode(env))


def corpus(name: str) -> dict[str, Any]:
    return json.loads((CORPUS / name).read_text(encoding="utf-8"))

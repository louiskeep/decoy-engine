"""Hand-built `decoy-publication/v1` documents for the descriptor tests.

Built with `json` and `hashlib` only, never with the engine codec, so a test that decodes one
checks the reader against the plan's wire rules (5.2), not against the writer.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

SCHEMA = "decoy-publication/v1"
RESULT_DOMAIN = b"decoy-publication-result/v1\x00"
MIB = 1 << 20


def canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def result_id(artifacts: Any) -> str:
    return hashlib.sha256(RESULT_DOMAIN + canonical(artifacts)).hexdigest()


def artifact(path: str = "customers.parquet", **over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": path,
        "role": "table",
        "rows": 3,
        "sha256": "2" * 64,
        "size": 640,
    }
    entry.update(over)
    return entry


def doc(**over: Any) -> dict[str, Any]:
    arts = [artifact("accounts.parquet", rows=2, sha256="1" * 64, size=512), artifact()]
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "unit": "immutable_directory/v1",
        "operation_id": "job-42.attempt:1",
        "result_id": result_id(arts),
        "artifacts": arts,
        "vault": {"sha256": "3" * 64, "size": 2048, "entry_count": 7},
        "engine_version": "0.22.0",
    }
    document.update(over)
    return document


def resign(document: dict[str, Any]) -> dict[str, Any]:
    """Recompute `result_id` after an artifact edit, so the edit is the only defect."""
    out = copy.deepcopy(document)
    out["result_id"] = result_id(out["artifacts"])
    return out


def encode(document: dict[str, Any]) -> bytes:
    """Canonical bytes of a raw document; values JSON cannot hold are written by `json` as is."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )

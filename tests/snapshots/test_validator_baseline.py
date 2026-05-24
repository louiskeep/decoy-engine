"""Snapshot harness for graph validation (V2.0-B).

Mirror of test_runner_baseline.py for the validator refactor. Lands
before V2.0-B starts splitting the bundled GraphConfigValidator into
focused modules, so each split step runs this harness and must produce
identical digests on the canonical fixtures. Drift in any fixture's
digest blocks the merge.

Per Dennis's standard (engineering-best-practices §1.1): snapshot
before extraction. The split is mechanical only if the snapshot agrees;
any output drift means the refactor changed behavior and must be
investigated before merge.

What's hashed:
  - result.errors: list[ValidationMessage] -> tuple of (code, path, message)
  - result.warnings: same shape
  - result.normalized_config: deep-stable JSON dump if present, else None

The hash is a single SHA-256 over the canonical-form JSON. One fixture
produces one golden file; a fixture either matches or it doesn't.

Adding a fixture:
  1. Define the graph config in FIXTURES below (a Python dict; the
     harness converts to YAML at runtime).
  2. Run: UPDATE_SNAPSHOTS=1 pytest tests/snapshots/test_validator_baseline.py
  3. Inspect the generated golden file under tests/snapshots/golden/
     validator/ and commit it alongside the fixture.

Removing a fixture:
  1. Delete the FIXTURES entry.
  2. Delete the matching file under tests/snapshots/golden/validator/.

Updating an existing fixture's expected output:
  Only do this when you have intentionally changed validation behavior.
  Re-run with UPDATE_SNAPSHOTS=1 and check in the new goldens. The
  commit MUST explain why validation legitimately changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from decoy_engine import validate_graph_full
from decoy_engine.validation_result import ValidationResult

GOLDEN = Path(__file__).parent / "golden" / "validator"


def _valid_minimal() -> dict[str, Any]:
    """One source + one mask + one target, all explicit formats."""
    return {
        "mode": "graph",
        "nodes": [
            {
                "id": "src",
                "kind": "source.file",
                "config": {"path": "/tmp/in.csv", "format": "csv"},
            },
            {
                "id": "mask1",
                "kind": "mask",
                "config": {"columns": {"name": {"strategy": "hash"}}},
            },
            {
                "id": "tgt",
                "kind": "target.file",
                "config": {"output_filename": "/tmp/out.csv", "format": "csv"},
            },
        ],
        "edges": [
            {"from": "src", "to": "mask1"},
            {"from": "mask1", "to": "tgt"},
        ],
    }


def _valid_format_backfill() -> dict[str, Any]:
    """Target has no explicit format; lenient mode back-fills from source."""
    return {
        "mode": "graph",
        "nodes": [
            {
                "id": "src",
                "kind": "source.file",
                "config": {"path": "/tmp/in.parquet", "format": "parquet"},
            },
            {
                "id": "tgt",
                "kind": "target.file",
                "config": {"output_filename": "/tmp/out.parquet"},
            },
        ],
        "edges": [{"from": "src", "to": "tgt"}],
    }


def _invalid_bad_mode() -> dict[str, Any]:
    return {"mode": "table", "nodes": [{"id": "x", "kind": "source.file"}]}


def _invalid_bad_kind() -> dict[str, Any]:
    return {
        "mode": "graph",
        "nodes": [{"id": "x", "kind": "nope.bogus", "config": {}}],
    }


def _invalid_cycle() -> dict[str, Any]:
    return {
        "mode": "graph",
        "nodes": [
            {"id": "a", "kind": "source.file", "config": {"path": "/tmp/a.csv", "format": "csv"}},
            {"id": "b", "kind": "mask", "config": {"columns": {}}},
            {"id": "c", "kind": "mask", "config": {"columns": {}}},
        ],
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "c"},
            {"from": "c", "to": "b"},
        ],
    }


def _invalid_mask_missing_column() -> dict[str, Any]:
    """mask references a column the source can't be proven to produce."""
    return {
        "mode": "graph",
        "nodes": [
            {
                "id": "src",
                "kind": "source.file",
                "config": {
                    "path": "/tmp/in.csv",
                    "format": "csv",
                    "column_names": ["a", "b"],
                },
            },
            {
                "id": "m",
                "kind": "mask",
                "config": {"columns": {"z": {"strategy": "hash"}}},
            },
        ],
        "edges": [{"from": "src", "to": "m"}],
    }


def _invalid_multiple_node_errors() -> dict[str, Any]:
    """Two bad nodes; collecting-mode should surface both."""
    return {
        "mode": "graph",
        "nodes": [
            {"id": "n1"},
            {"kind": "source.file"},
        ],
    }


FIXTURES: dict[str, dict[str, Any]] = {
    "valid_minimal": _valid_minimal(),
    "valid_format_backfill": _valid_format_backfill(),
    "invalid_bad_mode": _invalid_bad_mode(),
    "invalid_bad_kind": _invalid_bad_kind(),
    "invalid_cycle": _invalid_cycle(),
    "invalid_mask_missing_column": _invalid_mask_missing_column(),
    "invalid_multiple_node_errors": _invalid_multiple_node_errors(),
}


def _result_to_canonical_dict(result: ValidationResult) -> dict[str, Any]:
    """Serialize a ValidationResult to a comparison-stable dict.

    sort_keys downstream in json.dumps handles dict-key order. Lists keep
    their order (validator promises stable order, so reordering would be
    a regression worth catching). Each ValidationMessage becomes a tuple
    of (code, path, message) -- enough to detect a behavior change
    without coupling to the message-object internals.
    """

    def _msg(m: Any) -> dict[str, Any]:
        return {
            "code": getattr(m, "code", None),
            "path": getattr(m, "path", None),
            "message": getattr(m, "message", None) or getattr(m, "raw_message", None),
            "severity": getattr(m, "severity", None),
        }

    return {
        "errors": [_msg(e) for e in (result.errors or [])],
        "warnings": [_msg(w) for w in (result.warnings or [])],
        "normalized_config": result.normalized_config,
    }


def _digest(result: ValidationResult) -> str:
    payload = _result_to_canonical_dict(result)
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _golden_path(name: str) -> Path:
    return GOLDEN / f"{name}.sha256"


@pytest.mark.parametrize("name", sorted(FIXTURES.keys()))
def test_validator_snapshot(name: str) -> None:
    config = FIXTURES[name]
    yaml_text = yaml.safe_dump(config, sort_keys=False)
    result = validate_graph_full(yaml_text)
    digest = _digest(result)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        _golden_path(name).write_text(digest + "\n", encoding="utf-8")
        return

    path = _golden_path(name)
    if not path.exists():
        pytest.fail(
            f"Missing golden for fixture {name!r}. "
            f"Run with UPDATE_SNAPSHOTS=1 to create it, then inspect "
            f"{path} before committing."
        )
    expected = path.read_text(encoding="utf-8").strip()
    if expected != digest:
        # Render the canonical dict for the failure message so a human
        # can see what changed without re-running with UPDATE_SNAPSHOTS.
        actual_payload = _result_to_canonical_dict(result)
        pytest.fail(
            f"Validator output drift on fixture {name!r}\n"
            f"  expected digest: {expected}\n"
            f"  actual digest:   {digest}\n"
            f"  actual payload:  {json.dumps(actual_payload, indent=2, default=str)[:2000]}"
        )

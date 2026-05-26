"""PERF.BASE.1: end-to-end integration test for engine-side instrumentation.

Exercises a full graph run with a mask op and verifies that:
1. NodeRunRecord includes peak_memory_kb (per-node memory delta).
2. Mask node exports include `timings_per_strategy` with per-strategy
   counts + elapsed totals.
3. Per-strategy timings reach the manifest via the existing exports
   plumbing (no new manifest-emission code required).
4. Pipelines without timing enabled still run unchanged (zero behavior
   change for non-instrumented callers).
"""

from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest
import yaml

from decoy_engine import run_graph, validate_graph


@pytest.fixture
def csv_with_pii():
    """Five-row CSV with PII-shaped columns the mask op can transform."""
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "in.csv")
    out = os.path.join(tmpdir, "out.csv")
    df = pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5],
            "ssn": ["111-22-3333", "222-33-4444", "333-44-5555", "444-55-6666", "555-66-7777"],
            "email": [
                "alice@example.com",
                "bob@example.com",
                "carol@example.com",
                "dave@example.com",
                "eve@example.com",
            ],
            "amount": [100, 200, 300, 400, 500],
        }
    )
    df.to_csv(src, index=False)
    return src, out


def _yaml(d: dict) -> str:
    return yaml.safe_dump(d)


def test_mask_node_run_record_includes_peak_memory_kb(csv_with_pii):
    """Per-node memory delta is captured in NodeRunRecord for every node."""
    src, out = csv_with_pii
    cfg = _yaml(
        {
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": src}},
                {
                    "id": "m",
                    "kind": "mask",
                    "config": {
                        "seed": 42,
                        "columns": {
                            "ssn": {"strategy": "redact"},
                            "email": {"strategy": "redact"},
                        },
                    },
                },
                {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
            ],
            "edges": [
                {"from": "s", "to": "m"},
                {"from": "m", "to": "t"},
            ],
        }
    )
    validate_graph(cfg)
    result = run_graph(cfg)

    assert result["success"] is True

    # Every node record should have peak_memory_kb (always present per the
    # PERF.BASE.1 schema extension; >= 0).
    for record in result["nodes"]:
        assert "peak_memory_kb" in record, (
            f"node {record['node_id']!r} missing peak_memory_kb"
        )
        assert record["peak_memory_kb"] >= 0


def test_mask_node_exports_timings_per_strategy(csv_with_pii):
    """The mask op surfaces per-strategy timings via ctx.export, which lands
    in the NodeRunRecord exports field."""
    src, out = csv_with_pii
    cfg = _yaml(
        {
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": src}},
                {
                    "id": "m",
                    "kind": "mask",
                    "config": {
                        "seed": 42,
                        "columns": {
                            "ssn": {"strategy": "redact"},
                            "email": {"strategy": "redact"},
                        },
                    },
                },
                {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
            ],
            "edges": [
                {"from": "s", "to": "m"},
                {"from": "m", "to": "t"},
            ],
        }
    )
    validate_graph(cfg)
    result = run_graph(cfg)

    mask_record = next(r for r in result["nodes"] if r["node_id"] == "m")
    exports = mask_record.get("exports") or {}

    assert "timings_per_strategy" in exports, (
        f"mask node exports missing timings_per_strategy: keys={list(exports)}"
    )

    timings = exports["timings_per_strategy"]
    # Two columns masked with redact; should aggregate to one entry with count=2.
    assert "redact" in timings
    assert timings["redact"]["count"] == 2
    assert timings["redact"]["total_ms"] >= 0
    assert timings["redact"]["max_ms"] >= 0
    assert timings["redact"]["peak_delta_kb"] >= 0


def test_no_mask_strategies_means_no_timings_export(csv_with_pii):
    """An all-passthrough or empty mask config produces no per-strategy
    timings (the export is omitted, not present-but-empty)."""
    src, out = csv_with_pii
    cfg = _yaml(
        {
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": src}},
                # Empty columns mapping = no rules = no per-strategy work.
                {"id": "m", "kind": "mask", "config": {"columns": {}}},
                {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
            ],
            "edges": [
                {"from": "s", "to": "m"},
                {"from": "m", "to": "t"},
            ],
        }
    )
    validate_graph(cfg)
    result = run_graph(cfg)

    mask_record = next(r for r in result["nodes"] if r["node_id"] == "m")
    exports = mask_record.get("exports") or {}
    # No strategies fired, so the timings export is absent.
    assert "timings_per_strategy" not in exports


def test_node_record_schema_backward_compatible_for_non_mask_nodes(csv_with_pii):
    """Source and target nodes carry peak_memory_kb but no timings_per_strategy
    export (the latter is mask-specific)."""
    src, out = csv_with_pii
    cfg = _yaml(
        {
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": src}},
                {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
            ],
            "edges": [{"from": "s", "to": "t"}],
        }
    )
    validate_graph(cfg)
    result = run_graph(cfg)

    for record in result["nodes"]:
        assert "peak_memory_kb" in record
        exports = record.get("exports") or {}
        assert "timings_per_strategy" not in exports

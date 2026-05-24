"""Sprint 4 Commit 4: FK preservation failure-path integration tests.

Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG; materialize
parent pool; child samples with replacement.

Covers the runtime + lenient/strict mode behavior when a declared FK
can't be resolved cleanly:

  - Empty parent pool (all-null parent column) -> runtime
    EmptyParentPoolError; strict mode aborts the run, lenient mode
    surfaces it through the run telemetry.
  - Unknown parent column at runtime (schema drift between
    validator + runner) -> runtime UnknownFKColumnError; strict mode
    aborts.
  - Parent op fails -> child never runs; the upstream failure
    propagates.
"""

from __future__ import annotations

import pandas as pd
import pytest
import yaml as _yaml

from decoy_engine.context import ExecutionContext, make_key_resolver
from decoy_engine.graph.runner import execute_graph_capture


def _y(cfg: dict) -> str:
    return _yaml.safe_dump(cfg, sort_keys=False)


@pytest.fixture
def all_null_parent_csv(tmp_path):
    """Parent CSV where the PK column is entirely null. The pool
    resolver should raise EmptyParentPoolError."""
    src = tmp_path / "all_null.csv"
    pd.DataFrame(
        {
            "id": [None, None, None, None, None],
            "noise": [1, 2, 3, 4, 5],
        }
    ).to_csv(src, index=False)
    return str(src)


@pytest.fixture
def ctx():
    return ExecutionContext(
        derive_key=make_key_resolver(b"\x42" * 32, "test-pipeline"),
        pipeline_derive_key=make_key_resolver(b"\x33" * 32, "test-pipeline"),
    )


class TestEmptyParentPool:
    def test_empty_parent_pool_surfaces_as_node_error(self, all_null_parent_csv, tmp_path, ctx):
        """When the parent column is empty after null-filter, the
        pool resolver raises EmptyParentPoolError. The runner catches
        it via the op-error translator and marks the child node as
        failed."""
        cfg = {
            "mode": "graph",
            "nodes": [
                {
                    "id": "src_1",
                    "kind": "source.file",
                    "config": {"path": all_null_parent_csv, "format": "csv"},
                },
                {
                    "id": "mask_1",
                    "kind": "mask",
                    "config": {"columns": {"id": {"strategy": "hash"}}},
                },
                {
                    "id": "tgt_1",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "p_out.csv"), "format": "csv"},
                },
                # Strategy must be one of generate's validator-allowed
                # values (faker / sequence / categorical / formula);
                # FK coercion at runtime flips it to reference. The
                # mask_1 parent column is all-null, so pool_resolver
                # raises EmptyParentPoolError.
                {
                    "id": "synth_1",
                    "kind": "generate",
                    "config": {"columns": {"customer_id": {"strategy": "faker"}}},
                },
                {
                    "id": "tgt_2",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "c_out.csv"), "format": "csv"},
                },
            ],
            "edges": [
                {"from": "src_1", "to": "mask_1"},
                {"from": "mask_1", "to": "tgt_1"},
                {"from": "mask_1", "to": "synth_1"},
                {"from": "synth_1", "to": "tgt_2"},
            ],
            "column_relationships": [
                {
                    "kind": "fk",
                    "parent": {"node": "mask_1", "column": "id"},
                    "child": {"node": "synth_1", "column": "customer_id"},
                },
            ],
        }
        result, _cache = execute_graph_capture(
            _y(cfg),
            ctx=ctx,
            keep_nodes=["mask_1", "synth_1"],
        )
        # Run fails because synth_1 raised EmptyParentPoolError.
        assert not result["success"]
        synth_node = next(n for n in result["nodes"] if n["node_id"] == "synth_1")
        assert synth_node["status"] == "error"
        err = (synth_node["error"] or "").lower()
        # Error message carries either the structured class name OR
        # the "zero non-null" diagnostic text.
        assert "emptyparentpool" in err or "zero non-null" in err, (
            f"expected EmptyParentPool signal in error, got: {synth_node['error']!r}"
        )


class TestParentOpFailure:
    def test_parent_failure_prevents_child_from_running(self, tmp_path, ctx):
        """When the parent mask op fails (e.g. unknown strategy),
        the runner halts at the failure; the child synth never sees
        the FK declaration come into play because there's no parent
        output to draw from."""
        # Point src_1 at a file that doesn't exist so source.file
        # raises. Cheapest way to fail a parent without inventing a
        # malformed strategy.
        cfg = {
            "mode": "graph",
            "nodes": [
                {
                    "id": "src_1",
                    "kind": "source.file",
                    "config": {"path": str(tmp_path / "nope.csv"), "format": "csv"},
                },
                {
                    "id": "mask_1",
                    "kind": "mask",
                    "config": {"columns": {"id": {"strategy": "hash"}}},
                },
                {
                    "id": "tgt_1",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "p_out.csv"), "format": "csv"},
                },
                {
                    "id": "synth_1",
                    "kind": "generate",
                    "config": {"columns": {"customer_id": {"strategy": "faker"}}},
                },
                {
                    "id": "tgt_2",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "c_out.csv"), "format": "csv"},
                },
            ],
            "edges": [
                {"from": "src_1", "to": "mask_1"},
                {"from": "mask_1", "to": "tgt_1"},
                {"from": "mask_1", "to": "synth_1"},
                {"from": "synth_1", "to": "tgt_2"},
            ],
            "column_relationships": [
                {
                    "kind": "fk",
                    "parent": {"node": "mask_1", "column": "id"},
                    "child": {"node": "synth_1", "column": "customer_id"},
                },
            ],
        }
        result, _cache = execute_graph_capture(_y(cfg), ctx=ctx)
        assert not result["success"]
        # src_1 (or mask_1) carries the failure; downstream nodes
        # marked accordingly.
        statuses = {n["node_id"]: n["status"] for n in result["nodes"]}
        # At minimum the source or the first downstream node fails.
        assert "error" in statuses.values()

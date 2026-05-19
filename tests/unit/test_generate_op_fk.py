"""Sprint 4 Commit 4: unit tests for generate_op FK coercion.

Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG; materialize
parent pool; child samples with replacement.

Covers the FK preservation logic inside generate_op.apply at the op
level -- no graph runner, no file I/O. The integration tests in
tests/integration/test_fk_preservation_matrix.py cover the full
four-case matrix end-to-end; these tests target the op-level mechanics:

  - FK column values come from the pool (not from the declared strategy).
  - strategy_coerced flag is set correctly.
  - fk_preservation dict is exported via ctx.export.
  - pool_resolver error propagates cleanly.
"""
from __future__ import annotations

import pytest

from decoy_engine.context import ExecutionContext
from decoy_engine.graph.ops import generate_op


def _make_ctx(
    node_id: str,
    pool: list,
    parent_node: str = "mask_1",
    parent_col: str = "id",
) -> ExecutionContext:
    """Build a context with a pool_resolver and one FK declaration."""

    def resolver(nid: str, col: str) -> list:
        return pool

    ctx = ExecutionContext(pool_resolver=resolver)
    ctx.column_relationships = [
        {
            "kind": "fk",
            "parent": {"node": parent_node, "column": parent_col},
            "child":  {"node": node_id,    "column": "customer_id"},
        }
    ]
    ctx._current_node_id = node_id
    return ctx


class TestFKCoercion:
    def test_fk_column_values_come_from_pool(self):
        pool = [10, 20, 30]
        ctx = _make_ctx("synth_1", pool)
        config = {
            "row_count": 9,
            "columns": {
                "customer_id": {"strategy": "faker"},
            },
        }
        result = generate_op.apply([], config, ctx)
        assert len(result) == 9
        assert set(result["customer_id"].dropna().tolist()).issubset(set(pool))

    def test_strategy_coerced_true_when_original_not_reference(self):
        pool = [1, 2, 3]
        ctx = _make_ctx("synth_1", pool)
        config = {
            "row_count": 5,
            "columns": {
                "customer_id": {"strategy": "faker"},
            },
        }
        generate_op.apply([], config, ctx)
        metrics = ctx._exports.get("synth_1", {}).get("fk_preservation", {})
        assert "customer_id" in metrics
        assert metrics["customer_id"]["strategy_coerced"] is True
        assert metrics["customer_id"]["original_strategy"] == "faker"

    def test_strategy_coerced_false_when_already_reference(self):
        pool = [1, 2, 3]
        ctx = _make_ctx("synth_1", pool)
        config = {
            "row_count": 5,
            "columns": {
                "customer_id": {
                    "strategy": "reference",
                    "reference_table": "mask_1",
                    "reference_column": "id",
                },
            },
        }
        generate_op.apply([], config, ctx)
        metrics = ctx._exports.get("synth_1", {}).get("fk_preservation", {})
        assert "customer_id" in metrics
        assert metrics["customer_id"]["strategy_coerced"] is False

    def test_fk_preservation_exported_with_correct_keys(self):
        pool = [100, 200]
        ctx = _make_ctx("synth_1", pool)
        config = {
            "row_count": 4,
            "columns": {
                "customer_id": {"strategy": "faker"},
            },
        }
        generate_op.apply([], config, ctx)
        exports = ctx._exports.get("synth_1", {})
        assert "fk_preservation" in exports
        m = exports["fk_preservation"]["customer_id"]
        assert m["parent_node"] == "mask_1"
        assert m["parent_column"] == "id"
        assert m["child_column"] == "customer_id"
        assert m["pool_size"] == 2

    def test_non_fk_column_absent_from_metrics(self):
        pool = [1, 2]
        ctx = _make_ctx("synth_1", pool)
        config = {
            "row_count": 3,
            "columns": {
                "customer_id": {"strategy": "faker"},
                "seq_col": {"strategy": "sequence", "start": 1, "step": 1},
            },
        }
        generate_op.apply([], config, ctx)
        metrics = ctx._exports.get("synth_1", {}).get("fk_preservation", {})
        assert "customer_id" in metrics
        assert "seq_col" not in metrics

    def test_no_fk_coercion_when_node_id_does_not_match(self):
        """column_relationships entry for a different node is not applied here."""
        pool = [99]
        ctx = _make_ctx("synth_1", pool)
        # Pretend this op is a different node.
        ctx._current_node_id = "synth_2"
        config = {
            "row_count": 3,
            "columns": {
                # FK relationship targets synth_1, not synth_2 -- no coercion.
                "seq_col": {"strategy": "sequence", "start": 1, "step": 1},
            },
        }
        result = generate_op.apply([], config, ctx)
        assert list(result["seq_col"]) == [1, 2, 3]
        exports = ctx._exports.get("synth_2", {})
        assert "fk_preservation" not in exports

    def test_pool_resolver_error_propagates(self):
        from decoy_engine.exceptions import EmptyParentPoolError

        def bad_resolver(nid: str, col: str) -> list:
            raise EmptyParentPoolError(
                "pool empty", parent_node=nid, parent_column=col
            )

        ctx = ExecutionContext(pool_resolver=bad_resolver)
        ctx.column_relationships = [
            {
                "kind": "fk",
                "parent": {"node": "mask_1", "column": "id"},
                "child":  {"node": "synth_1", "column": "customer_id"},
            }
        ]
        ctx._current_node_id = "synth_1"
        config = {
            "row_count": 3,
            "columns": {"customer_id": {"strategy": "faker"}},
        }
        with pytest.raises(EmptyParentPoolError):
            generate_op.apply([], config, ctx)

    def test_dropped_rows_count_in_metrics(self):
        """Rows dropped by the FK drop-pass are recorded in fk_preservation metrics."""
        # pool with two real values + an empty sentinel; the drop-pass
        # filters rows where the FK column resolved to None.
        # Achieve this by returning a pool that ColumnGenerator will
        # sample from without dropping -- then directly test the metrics
        # shape for dropped_rows (the integration test owns the
        # full-sentinel-row scenario end-to-end).
        pool = [1, 2, 3]
        ctx = _make_ctx("synth_1", pool)
        config = {
            "row_count": 5,
            "columns": {
                "customer_id": {"strategy": "faker"},
            },
        }
        result = generate_op.apply([], config, ctx)
        metrics = ctx._exports.get("synth_1", {}).get("fk_preservation", {})
        # With a healthy pool no rows are dropped.
        assert metrics["customer_id"]["dropped_rows"] == 0
        assert len(result) == 5

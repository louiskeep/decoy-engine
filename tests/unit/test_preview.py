"""Unit tests for graph.preview: PreviewPolicy and execute_preview."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from decoy_engine.graph.preview import PreviewPolicy, execute_preview


def _make_op(result=None, side_effect=None):
    op = MagicMock()
    op.OUTPUT_KIND = None
    op.OUTPUT_PORTS = ()
    if side_effect is not None:
        op.apply.side_effect = side_effect
    else:
        op.apply.return_value = result
    return op


def _run(nodes, edges, policy, ops_map):
    with (
        patch("decoy_engine.graph.ops.OPS", ops_map),
        patch("decoy_engine.graph.registry.native_engine_for", return_value="pandas"),
    ):
        return execute_preview(nodes, edges, policy, None, "pandas")


class TestPreviewPolicy(unittest.TestCase):
    def test_defaults(self):
        p = PreviewPolicy(node_id="n1")
        assert p.row_limit == 50
        assert p.skip_targets is True

    def test_custom_values(self):
        p = PreviewPolicy(node_id="n1", row_limit=10, skip_targets=False)
        assert p.node_id == "n1"
        assert p.row_limit == 10
        assert p.skip_targets is False


class TestExecutePreview(unittest.TestCase):
    def test_single_node_returns_rows(self):
        df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
        nodes = [{"id": "n1", "kind": "source.file"}]
        policy = PreviewPolicy(node_id="n1", row_limit=50)
        result = _run(nodes, [], policy, {"source.file": _make_op(df)})
        assert result["row_count"] == 2
        assert result["columns"] == ["a", "b"]
        assert result["error"] is None

    def test_row_limit_applied(self):
        df = pd.DataFrame({"a": list(range(100))})
        nodes = [{"id": "n1", "kind": "source.file"}]
        policy = PreviewPolicy(node_id="n1", row_limit=5)
        result = _run(nodes, [], policy, {"source.file": _make_op(df)})
        assert result["row_count"] <= 5

    def test_applied_chain_in_topo_order(self):
        nodes = [
            {"id": "src", "kind": "source.file"},
            {"id": "msk", "kind": "mask"},
        ]
        edges = [{"from": "src", "to": "msk"}]
        policy = PreviewPolicy(node_id="msk")
        result = _run(nodes, edges, policy, {
            "source.file": _make_op(pd.DataFrame({"a": [1]})),
            "mask": _make_op(pd.DataFrame({"a": [99]})),
        })
        assert result["applied_chain"] == ["src", "msk"]

    def test_null_op_result_returns_no_data(self):
        nodes = [{"id": "n1", "kind": "source.file"}]
        policy = PreviewPolicy(node_id="n1")
        result = _run(nodes, [], policy, {"source.file": _make_op(None)})
        assert result["row_count"] == 0
        assert result["error"] == "no data produced"

    def test_error_in_node_captured(self):
        nodes = [{"id": "n1", "kind": "source.file"}]
        policy = PreviewPolicy(node_id="n1")
        result = _run(nodes, [], policy, {
            "source.file": _make_op(side_effect=RuntimeError("read failed")),
        })
        assert result["row_count"] == 0
        assert "failed" in (result["error"] or "")

    def test_error_names_failing_node(self):
        nodes = [{"id": "bad_src", "kind": "source.file"}]
        policy = PreviewPolicy(node_id="bad_src")
        result = _run(nodes, [], policy, {
            "source.file": _make_op(side_effect=RuntimeError("oops")),
        })
        assert "bad_src" in (result["error"] or "")

    def test_flag_pause_captured_not_raised(self):
        from decoy_engine.exceptions import FlagPauseSignal
        nodes = [{"id": "gate", "kind": "flag_gate"}]
        policy = PreviewPolicy(node_id="gate")
        result = _run(nodes, [], policy, {
            "flag_gate": _make_op(side_effect=FlagPauseSignal("pause")),
        })
        assert result["row_count"] == 0
        assert "gate blocked" in (result["error"] or "")

    def test_target_node_skipped_by_default(self):
        df = pd.DataFrame({"a": [1, 2]})
        target_op = _make_op(df)
        nodes = [
            {"id": "src", "kind": "source.file"},
            {"id": "tgt", "kind": "target.file"},
        ]
        edges = [{"from": "src", "to": "tgt"}]
        policy = PreviewPolicy(node_id="tgt", skip_targets=True)
        result = _run(nodes, edges, policy, {
            "source.file": _make_op(df),
            "target.file": target_op,
        })
        target_op.apply.assert_not_called()
        assert result["row_count"] == 0
        assert "skipped" in (result["error"] or "")

    def test_target_skip_error_names_node(self):
        df = pd.DataFrame({"a": [1]})
        nodes = [
            {"id": "src", "kind": "source.file"},
            {"id": "my_tgt", "kind": "target.file"},
        ]
        edges = [{"from": "src", "to": "my_tgt"}]
        policy = PreviewPolicy(node_id="my_tgt", skip_targets=True)
        result = _run(nodes, edges, policy, {
            "source.file": _make_op(df),
            "target.file": _make_op(df),
        })
        assert "my_tgt" in (result["error"] or "")

    def test_target_not_skipped_when_policy_disabled(self):
        df = pd.DataFrame({"a": [1, 2]})
        target_op = _make_op(df)
        nodes = [
            {"id": "src", "kind": "source.file"},
            {"id": "tgt", "kind": "target.file"},
        ]
        edges = [{"from": "src", "to": "tgt"}]
        policy = PreviewPolicy(node_id="tgt", skip_targets=False)
        result = _run(nodes, edges, policy, {
            "source.file": _make_op(df),
            "target.file": target_op,
        })
        target_op.apply.assert_called_once()
        assert result["row_count"] == 2

    def test_node_id_in_result(self):
        nodes = [{"id": "src42", "kind": "source.file"}]
        policy = PreviewPolicy(node_id="src42")
        result = _run(nodes, [], policy, {
            "source.file": _make_op(pd.DataFrame({"x": [1]})),
        })
        assert result["node_id"] == "src42"

    def test_elapsed_ms_present(self):
        nodes = [{"id": "n1", "kind": "source.file"}]
        policy = PreviewPolicy(node_id="n1")
        result = _run(nodes, [], policy, {
            "source.file": _make_op(pd.DataFrame({"x": [1]})),
        })
        assert isinstance(result["elapsed_ms"], int)
        assert result["elapsed_ms"] >= 0


if __name__ == "__main__":
    unittest.main()

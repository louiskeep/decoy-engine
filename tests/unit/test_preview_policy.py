"""Tests for PreviewPolicy and execute_preview.

All tests inject mock ops via unittest.mock.patch rather than registering
real op kinds. Mock ops return pandas DataFrames so GraphCache can convert
them to Arrow via the normal conversion path.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from decoy_engine.exceptions import FlagPauseSignal
from decoy_engine.graph.planner import build_plan, build_preview_plan
from decoy_engine.graph.preview import PreviewPolicy, execute_preview


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _df(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame({"x": list(range(n))})


def _op(
    result=None,
    has_side_effects: bool = False,
    output_kind: str = "stream",
    output_ports: tuple = (),
) -> MagicMock:
    op = MagicMock()
    op.HAS_SIDE_EFFECTS = has_side_effects
    op.OUTPUT_KIND = output_kind
    op.OUTPUT_PORTS = output_ports
    op.NATIVE_ENGINE = "pandas"
    op.apply.return_value = _df() if result is None else result
    return op


def _cfg(nodes, edges=None) -> dict:
    return {"mode": "graph", "nodes": nodes, "edges": edges or []}


@contextmanager
def _ops(ops_map: dict):
    """Patch OPS and native_engine_for for the duration of the block."""
    with (
        patch("decoy_engine.graph.ops.OPS", ops_map),
        patch(
            "decoy_engine.graph.registry.native_engine_for",
            lambda kind, mode="pandas": "pandas",
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# PreviewPolicy defaults
# ---------------------------------------------------------------------------

class TestPreviewPolicy:
    def test_defaults(self):
        p = PreviewPolicy(node_id="n1")
        assert p.row_limit == 50
        assert p.side_effect_policy == "skip"
        assert p.on_error == "capture"

    def test_custom_values_preserved(self):
        p = PreviewPolicy(node_id="n1", row_limit=10, side_effect_policy="allow", on_error="abort")
        assert p.node_id == "n1"
        assert p.row_limit == 10
        assert p.side_effect_policy == "allow"
        assert p.on_error == "abort"


# ---------------------------------------------------------------------------
# row_limit clamping
# ---------------------------------------------------------------------------

class TestRowLimit:
    def test_clamped_to_one_minimum(self):
        cfg = _cfg([{"id": "n1", "kind": "k", "config": {}}])
        with _ops({"k": _op(result=_df(10))}):
            result = execute_preview(cfg, PreviewPolicy(node_id="n1", row_limit=0))
        assert result["row_count"] == 1

    def test_clamped_to_thousand_maximum(self):
        cfg = _cfg([{"id": "n1", "kind": "k", "config": {}}])
        with _ops({"k": _op(result=_df(2000))}):
            result = execute_preview(cfg, PreviewPolicy(node_id="n1", row_limit=9999))
        assert result["row_count"] == 1000

    def test_injected_into_node_cfg(self):
        captured = []

        def capturing_apply(inputs, cfg, ctx):
            captured.append(cfg.get("__preview_row_limit"))
            return _df()

        op = _op()
        op.apply.side_effect = capturing_apply
        cfg = _cfg([{"id": "n1", "kind": "k", "config": {}}])
        with _ops({"k": op}):
            execute_preview(cfg, PreviewPolicy(node_id="n1", row_limit=25))
        assert captured == [25]


# ---------------------------------------------------------------------------
# side_effect_policy
# ---------------------------------------------------------------------------

class TestSideEffectPolicy:
    def test_skip_does_not_call_side_effecting_target(self):
        sink = _op(has_side_effects=True)
        cfg = _cfg([{"id": "sink", "kind": "k", "config": {}}])
        with _ops({"k": sink}):
            execute_preview(cfg, PreviewPolicy(node_id="sink", side_effect_policy="skip"))
        sink.apply.assert_not_called()

    def test_skip_sets_error_naming_has_side_effects(self):
        sink = _op(has_side_effects=True)
        cfg = _cfg([{"id": "sink", "kind": "target.csv", "config": {}}])
        with _ops({"target.csv": sink}):
            result = execute_preview(cfg, PreviewPolicy(node_id="sink", side_effect_policy="skip"))
        assert result["error"] is not None
        assert "HAS_SIDE_EFFECTS" in result["error"]

    def test_skip_records_bypassed_node_in_skipped_nodes(self):
        sink = _op(has_side_effects=True)
        cfg = _cfg([{"id": "sink", "kind": "k", "config": {}}])
        with _ops({"k": sink}):
            result = execute_preview(cfg, PreviewPolicy(node_id="sink"))
        assert "sink" in result["skipped_nodes"]

    def test_allow_calls_side_effecting_op(self):
        sink = _op(has_side_effects=True, result=_df(3))
        cfg = _cfg([{"id": "sink", "kind": "k", "config": {}}])
        with _ops({"k": sink}):
            result = execute_preview(cfg, PreviewPolicy(node_id="sink", side_effect_policy="allow"))
        sink.apply.assert_called_once()
        assert result["skipped_nodes"] == []

    def test_upstream_side_effecting_op_skipped_target_still_runs(self):
        """Skip a non-target side-effecting node; loop continues to target."""
        cfg = _cfg(
            nodes=[
                {"id": "src", "kind": "src", "config": {}},
                {"id": "mid", "kind": "mid", "config": {}},
                {"id": "tgt", "kind": "tgt", "config": {}},
            ],
            edges=[
                {"from": "src", "to": "mid"},
                {"from": "mid", "to": "tgt"},
            ],
        )
        src_op = _op(result=_df(5))
        mid_op = _op(has_side_effects=True, result=_df(5))
        tgt_op = _op(result=_df(3))
        with _ops({"src": src_op, "mid": mid_op, "tgt": tgt_op}):
            result = execute_preview(cfg, PreviewPolicy(node_id="tgt", side_effect_policy="skip"))
        mid_op.apply.assert_not_called()
        tgt_op.apply.assert_called_once()
        assert "mid" in result["skipped_nodes"]

    def test_no_skipped_nodes_when_no_side_effects(self):
        cfg = _cfg([{"id": "n1", "kind": "k", "config": {}}])
        with _ops({"k": _op()}):
            result = execute_preview(cfg, PreviewPolicy(node_id="n1"))
        assert result["skipped_nodes"] == []


# ---------------------------------------------------------------------------
# on_error policy
# ---------------------------------------------------------------------------

class TestOnError:
    def test_capture_continues_after_upstream_error(self):
        cfg = _cfg(
            nodes=[
                {"id": "src", "kind": "src", "config": {}},
                {"id": "tgt", "kind": "tgt", "config": {}},
            ],
            edges=[{"from": "src", "to": "tgt"}],
        )
        src_op = _op()
        src_op.apply.side_effect = RuntimeError("boom")
        tgt_op = _op(result=_df(2))
        with _ops({"src": src_op, "tgt": tgt_op}):
            result = execute_preview(cfg, PreviewPolicy(node_id="tgt", on_error="capture"))
        tgt_op.apply.assert_called_once()
        assert result["error"] is not None

    def test_abort_stops_at_upstream_error(self):
        cfg = _cfg(
            nodes=[
                {"id": "src", "kind": "src", "config": {}},
                {"id": "tgt", "kind": "tgt", "config": {}},
            ],
            edges=[{"from": "src", "to": "tgt"}],
        )
        src_op = _op()
        src_op.apply.side_effect = RuntimeError("boom")
        tgt_op = _op(result=_df(2))
        with _ops({"src": src_op, "tgt": tgt_op}):
            result = execute_preview(cfg, PreviewPolicy(node_id="tgt", on_error="abort"))
        tgt_op.apply.assert_not_called()
        assert result["error"] is not None

    def test_target_error_always_stops(self):
        """Error on target stops regardless of on_error=capture."""
        cfg = _cfg([{"id": "tgt", "kind": "tgt", "config": {}}])
        tgt_op = _op()
        tgt_op.apply.side_effect = RuntimeError("target exploded")
        with _ops({"tgt": tgt_op}):
            result = execute_preview(cfg, PreviewPolicy(node_id="tgt", on_error="capture"))
        assert result["error"] is not None
        assert result["row_count"] == 0


# ---------------------------------------------------------------------------
# FlagPauseSignal
# ---------------------------------------------------------------------------

class TestFlagPauseSignal:
    def test_captured_not_reraised(self):
        gate_op = _op()
        gate_op.apply.side_effect = FlagPauseSignal("hold")
        cfg = _cfg([{"id": "gate", "kind": "gate", "config": {}}])
        with _ops({"gate": gate_op}):
            result = execute_preview(cfg, PreviewPolicy(node_id="gate"))
        assert result["error"] is not None
        assert "gate blocked" in result["error"]


# ---------------------------------------------------------------------------
# result structure
# ---------------------------------------------------------------------------

class TestPreviewResultStructure:
    def test_node_id_in_result(self):
        cfg = _cfg([{"id": "n1", "kind": "k", "config": {}}])
        with _ops({"k": _op(result=_df(3))}):
            result = execute_preview(cfg, PreviewPolicy(node_id="n1"))
        assert result["node_id"] == "n1"

    def test_applied_chain_matches_plan_order(self):
        cfg = _cfg(
            nodes=[
                {"id": "a", "kind": "src", "config": {}},
                {"id": "b", "kind": "tgt", "config": {}},
            ],
            edges=[{"from": "a", "to": "b"}],
        )
        plan = build_preview_plan(cfg, "b")
        with _ops({"src": _op(result=_df(2)), "tgt": _op(result=_df(2))}):
            result = execute_preview(cfg, PreviewPolicy(node_id="b"))
        assert result["applied_chain"] == plan.order

    def test_error_is_none_on_success(self):
        cfg = _cfg([{"id": "n1", "kind": "k", "config": {}}])
        with _ops({"k": _op(result=_df(3))}):
            result = execute_preview(cfg, PreviewPolicy(node_id="n1"))
        assert result["error"] is None

    def test_columns_and_rows_populated_on_success(self):
        cfg = _cfg([{"id": "n1", "kind": "k", "config": {}}])
        with _ops({"k": _op(result=pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}))}):
            result = execute_preview(cfg, PreviewPolicy(node_id="n1"))
        assert result["columns"] == ["a", "b"]
        assert len(result["rows"]) == 2


# ---------------------------------------------------------------------------
# split ops
# ---------------------------------------------------------------------------

class TestSplitOps:
    def test_split_op_exposes_pass_port_as_target_output(self):
        """Split ops expose the 'pass' port as the node's direct preview output."""
        pass_df = _df(3)
        fail_df = _df(7)
        split_op = MagicMock()
        split_op.HAS_SIDE_EFFECTS = False
        split_op.OUTPUT_KIND = "split"
        split_op.OUTPUT_PORTS = ("pass", "fail")
        split_op.NATIVE_ENGINE = "pandas"
        split_op.apply.return_value = {"pass": pass_df, "fail": fail_df}
        cfg = _cfg([{"id": "router", "kind": "if", "config": {}}])
        with _ops({"if": split_op}):
            result = execute_preview(cfg, PreviewPolicy(node_id="router"))
        assert result["row_count"] == 3  # "pass" port only


# ---------------------------------------------------------------------------
# preview plan order is a subset of the full plan order
# ---------------------------------------------------------------------------

class TestSharedPlanOrder:
    def test_preview_order_is_ancestor_subset_of_full_order(self):
        """build_preview_plan returns nodes in the same relative topo order
        as build_plan, scoped to the target's ancestor subgraph."""
        cfg = _cfg(
            nodes=[
                {"id": "a", "kind": "source.test", "config": {}},
                {"id": "b", "kind": "transform.test", "config": {}},
                {"id": "c", "kind": "target.test", "config": {}},
            ],
            edges=[
                {"from": "a", "to": "b"},
                {"from": "b", "to": "c"},
            ],
        )
        full_plan = build_plan(cfg)
        preview_plan = build_preview_plan(cfg, "b")
        assert set(preview_plan.order).issubset(set(full_plan.order))
        full_idx = {nid: i for i, nid in enumerate(full_plan.order)}
        preview_positions = [full_idx[nid] for nid in preview_plan.order]
        assert preview_positions == sorted(preview_positions)

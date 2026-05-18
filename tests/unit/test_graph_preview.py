"""Unit tests for graph.preview: PreviewPolicy and run_preview.

PreviewPolicy tests are pure dataclass tests with no external deps.
run_preview tests use monkeypatching to inject fake ops into OPS so
execution happens without file I/O or real connectors.

Fake op kinds used:
  test.source     -- zero-input source, returns DataFrame of n rows
  test.pass       -- single-input passthrough
  target.fake     -- side-effecting target; returns input when preview key set
  test.gate       -- raises FlagPauseSignal unconditionally
  test.error      -- raises RuntimeError unconditionally

Sprint 1.4 - Preview Policy Unification.
"""
from __future__ import annotations

import pytest

from decoy_engine.graph.preview import PreviewPolicy, run_preview


# ---------------------------------------------------------------------------
# Fake ops
# ---------------------------------------------------------------------------


class _FakeSourceOp:
    NATIVE_ENGINE = "pandas"
    INPUT_ARITY = (0, 0)
    OUTPUT_KIND = "stream"

    @staticmethod
    def apply(inputs, cfg, ctx):
        import pandas as pd
        n = cfg.get("n", 10)
        return pd.DataFrame({"x": range(n)})


class _FakePassOp:
    NATIVE_ENGINE = "pandas"
    INPUT_ARITY = (1, 1)
    OUTPUT_KIND = "stream"

    @staticmethod
    def apply(inputs, cfg, ctx):
        return inputs[0]


class _FakeTargetOp:
    """Simulates a side-effecting target. Returns data in preview mode."""
    NATIVE_ENGINE = "pandas"
    INPUT_ARITY = (1, 1)
    OUTPUT_KIND = "stream"

    @staticmethod
    def apply(inputs, cfg, ctx):
        if "__preview_row_limit" in cfg:
            return inputs[0]
        raise RuntimeError("side-effecting write attempted without preview policy")


class _FakeGateOp:
    NATIVE_ENGINE = "pandas"
    INPUT_ARITY = (0, 1)
    OUTPUT_KIND = "stream"

    @staticmethod
    def apply(inputs, cfg, ctx):
        from decoy_engine.exceptions import FlagPauseSignal
        raise FlagPauseSignal("test gate")


class _FakeErrorOp:
    NATIVE_ENGINE = "pandas"
    INPUT_ARITY = (0, 1)
    OUTPUT_KIND = "stream"

    @staticmethod
    def apply(inputs, cfg, ctx):
        raise RuntimeError("deliberate test error")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ops(monkeypatch):
    from decoy_engine.graph import ops
    monkeypatch.setitem(ops.OPS, "test.source", _FakeSourceOp)
    monkeypatch.setitem(ops.OPS, "test.pass", _FakePassOp)
    monkeypatch.setitem(ops.OPS, "target.fake", _FakeTargetOp)
    monkeypatch.setitem(ops.OPS, "test.gate", _FakeGateOp)
    monkeypatch.setitem(ops.OPS, "test.error", _FakeErrorOp)


def _sub_cfg(nodes, edges=None):
    return {"mode": "graph", "nodes": nodes, "edges": edges or []}


# ---------------------------------------------------------------------------
# PreviewPolicy unit tests
# ---------------------------------------------------------------------------


class TestPreviewPolicy:
    def test_defaults(self):
        p = PreviewPolicy(target_node_id="n")
        assert p.row_limit == 50
        assert p.skip_side_effects is True
        assert p.on_upstream_error == "continue"

    def test_custom_values(self):
        p = PreviewPolicy(
            target_node_id="n",
            row_limit=10,
            skip_side_effects=False,
            on_upstream_error="stop",
        )
        assert p.row_limit == 10
        assert p.skip_side_effects is False
        assert p.on_upstream_error == "stop"

    def test_row_limit_zero_raises(self):
        with pytest.raises(ValueError, match="row_limit"):
            PreviewPolicy(target_node_id="n", row_limit=0)

    def test_row_limit_negative_raises(self):
        with pytest.raises(ValueError, match="row_limit"):
            PreviewPolicy(target_node_id="n", row_limit=-1)

    def test_on_upstream_error_invalid_raises(self):
        with pytest.raises(ValueError, match="on_upstream_error"):
            PreviewPolicy(target_node_id="n", on_upstream_error="ignore")

    def test_on_upstream_error_stop_accepted(self):
        p = PreviewPolicy(target_node_id="n", on_upstream_error="stop")
        assert p.on_upstream_error == "stop"

    def test_on_upstream_error_continue_accepted(self):
        p = PreviewPolicy(target_node_id="n", on_upstream_error="continue")
        assert p.on_upstream_error == "continue"

    def test_node_config_patch_has_row_limit(self):
        p = PreviewPolicy(target_node_id="n", row_limit=25)
        patch = p.node_config_patch()
        assert patch["__preview_row_limit"] == 25

    def test_frozen_immutable(self):
        p = PreviewPolicy(target_node_id="n")
        with pytest.raises((AttributeError, TypeError)):
            p.row_limit = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# run_preview tests
# ---------------------------------------------------------------------------


class TestRunPreview:
    def test_basic_result_shape(self, fake_ops):
        sub_cfg = _sub_cfg(
            nodes=[{"id": "src", "kind": "test.source", "config": {"n": 5}}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="src"), ctx=None)
        assert result["node_id"] == "src"
        assert isinstance(result["columns"], list)
        assert isinstance(result["rows"], list)
        assert result["elapsed_ms"] >= 0

    def test_row_count_equals_rows_length(self, fake_ops):
        sub_cfg = _sub_cfg(
            nodes=[{"id": "src", "kind": "test.source", "config": {"n": 5}}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="src"), ctx=None)
        assert result["row_count"] == len(result["rows"])

    def test_row_limit_caps_output(self, fake_ops):
        sub_cfg = _sub_cfg(
            nodes=[{"id": "src", "kind": "test.source", "config": {"n": 100}}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="src", row_limit=10), ctx=None)
        assert result["row_count"] <= 10

    def test_truncated_true_when_over_limit(self, fake_ops):
        sub_cfg = _sub_cfg(
            nodes=[{"id": "src", "kind": "test.source", "config": {"n": 100}}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="src", row_limit=10), ctx=None)
        assert result["truncated"] is True

    def test_truncated_false_when_within_limit(self, fake_ops):
        sub_cfg = _sub_cfg(
            nodes=[{"id": "src", "kind": "test.source", "config": {"n": 5}}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="src", row_limit=10), ctx=None)
        assert result["truncated"] is False

    def test_applied_chain_matches_plan_order(self, fake_ops):
        """applied_chain reflects build_plan's topo order, not arbitrary order."""
        sub_cfg = _sub_cfg(
            nodes=[
                {"id": "src", "kind": "test.source", "config": {"n": 3}},
                {"id": "pass", "kind": "test.pass", "config": {}},
            ],
            edges=[{"from": "src", "to": "pass"}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="pass"), ctx=None)
        assert result["applied_chain"] == ["src", "pass"]

    def test_passthrough_carries_data(self, fake_ops):
        """Data flows through a passthrough node unchanged."""
        sub_cfg = _sub_cfg(
            nodes=[
                {"id": "src", "kind": "test.source", "config": {"n": 7}},
                {"id": "pass", "kind": "test.pass", "config": {}},
            ],
            edges=[{"from": "src", "to": "pass"}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="pass"), ctx=None)
        assert result["row_count"] == 7
        assert result["error"] is None

    def test_error_captured_not_raised(self, fake_ops):
        sub_cfg = _sub_cfg(
            nodes=[{"id": "err", "kind": "test.error", "config": {}}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="err"), ctx=None)
        assert result["error"] is not None
        assert result["row_count"] == 0

    def test_upstream_error_continue_records_error_and_proceeds(self, fake_ops):
        """Default on_upstream_error='continue' captures upstream errors and
        continues execution; error_msg is set but the run does not halt early."""
        sub_cfg = _sub_cfg(
            nodes=[
                {"id": "err", "kind": "test.error", "config": {}},
                {"id": "pass", "kind": "test.pass", "config": {}},
            ],
            edges=[{"from": "err", "to": "pass"}],
        )
        # Default policy uses on_upstream_error="continue".
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="pass"), ctx=None)
        # Error from upstream is captured in the result.
        assert result["error"] is not None
        assert "err" in result["error"] or "failed" in result["error"]

    def test_upstream_error_stop_halts_run(self, fake_ops):
        """on_upstream_error='stop' halts at the first upstream failure."""
        sub_cfg = _sub_cfg(
            nodes=[
                {"id": "err", "kind": "test.error", "config": {}},
                {"id": "pass", "kind": "test.pass", "config": {}},
            ],
            edges=[{"from": "err", "to": "pass"}],
        )
        result = run_preview(
            sub_cfg,
            PreviewPolicy(target_node_id="pass", on_upstream_error="stop"),
            ctx=None,
        )
        assert result["error"] is not None
        assert result["row_count"] == 0

    def test_target_kind_sets_side_effect_suppressed(self, fake_ops):
        """target.* kinds get skip_reason='side-effect-suppressed'."""
        sub_cfg = _sub_cfg(
            nodes=[
                {"id": "src", "kind": "test.source", "config": {"n": 3}},
                {"id": "tgt", "kind": "target.fake", "config": {}},
            ],
            edges=[{"from": "src", "to": "tgt"}],
        )
        result = run_preview(
            sub_cfg,
            PreviewPolicy(target_node_id="tgt", skip_side_effects=True),
            ctx=None,
        )
        assert result["skip_reason"] == "side-effect-suppressed"

    def test_non_target_no_skip_reason(self, fake_ops):
        sub_cfg = _sub_cfg(
            nodes=[{"id": "src", "kind": "test.source", "config": {"n": 3}}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="src"), ctx=None)
        assert result["skip_reason"] is None

    def test_gate_blocked_sets_skip_reason(self, fake_ops):
        sub_cfg = _sub_cfg(
            nodes=[{"id": "gate", "kind": "test.gate", "config": {}}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="gate"), ctx=None)
        assert result["skip_reason"] == "gate-blocked"
        assert result["error"] is not None
        assert "gate blocked" in result["error"]

    def test_no_data_returns_empty_columns_and_rows(self, fake_ops):
        sub_cfg = _sub_cfg(
            nodes=[{"id": "err", "kind": "test.error", "config": {}}],
        )
        result = run_preview(sub_cfg, PreviewPolicy(target_node_id="err"), ctx=None)
        assert result["columns"] == []
        assert result["rows"] == []

"""Fit-time categorical-retention warn-gate (HC-5 D3).

Mirrors tests/unit/generation/test_fidelity_gate.py's shape: pure scorer
tests plus GlobalSettings/bare-dict-reader coverage. The gate scores a
`distribution-snapshot/v1` dict directly (no generation dependency), so
these tests build snapshots via `compute_distribution_snapshot` rather
than running a pipeline.
"""

from __future__ import annotations

import copy
import json

import pandas as pd
import pytest

from decoy_engine.quality._retention_gate import (
    DEFAULT_CATEGORICAL_RETENTION_WARN_THRESHOLD,
    categorical_retention_warn_threshold,
    score_categorical_retention,
    warn_on_low_categorical_retention,
)
from decoy_engine.quality.snapshot import compute_distribution_snapshot


class TestCliffCase:
    def test_freetext_column_scores_zero_and_warns(self):
        df = pd.DataFrame({"notes": [f"note number {i} with text" for i in range(50)]})
        snap = compute_distribution_snapshot(df)
        assert snap["columns"]["notes"]["kind"] == "freetext"
        warnings = score_categorical_retention(snap, threshold=0.8)
        assert len(warnings) == 1
        assert "categorical_retention_cardinality_cliff" in warnings[0]
        assert "column='notes'" in warnings[0]
        assert "score=0.0" in warnings[0]

    def test_threshold_zero_silences_cliff_case(self):
        df = pd.DataFrame({"notes": [f"note number {i} with text" for i in range(50)]})
        snap = compute_distribution_snapshot(df)
        assert score_categorical_retention(snap, threshold=0.0) == []

    def test_high_cardinality_column_never_hits_cliff(self):
        """The opt-in delivers full retention, so it never scores 0.0 even
        though its raw distinct count would otherwise force freetext."""
        df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(50)]})
        snap = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
        assert snap["columns"]["code"]["kind"] == "categorical"
        assert score_categorical_retention(snap, threshold=0.8) == []


class TestTopKCollapse:
    def test_low_retention_warns(self):
        # 26 distinct values total (<=30 cap, so this stays categorical, not
        # freetext); top_k=1 keeps only the dominant class' mass.
        df = pd.DataFrame(
            {"city": ["dominant"] * 10 + [f"c{i}" for i in range(25)]},
        )
        snap = compute_distribution_snapshot(df, categorical_top_k=1)
        col = snap["columns"]["city"]
        assert col["kind"] == "categorical"
        warnings = score_categorical_retention(snap, threshold=0.8)
        assert len(warnings) == 1
        assert "categorical_retention_below_threshold" in warnings[0]
        assert "column='city'" in warnings[0]

    def test_full_retention_below_cap_does_not_warn(self):
        # 5 distinct values, all kept (well under top_k default of 20).
        df = pd.DataFrame({"state": ["CA", "NY", "TX", "CA", "NY"]})
        snap = compute_distribution_snapshot(df)
        assert score_categorical_retention(snap, threshold=0.8) == []

    def test_high_cardinality_full_vocab_does_not_warn(self):
        df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(50)] * 2})
        snap = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
        assert score_categorical_retention(snap, threshold=0.999) == []

    def test_threshold_zero_silences_top_k_collapse(self):
        df = pd.DataFrame(
            {"city": ["dominant"] * 10 + [f"c{i}" for i in range(25)]},
        )
        snap = compute_distribution_snapshot(df, categorical_top_k=1)
        assert score_categorical_retention(snap, threshold=0.0) == []


class TestJointCollapse:
    def test_joint_collapse_warns(self):
        # Many (a, b) cells, one dominant pair; contingency_top_k=1 collapses
        # most of the joint mass into other_count.
        df = pd.DataFrame(
            {
                "a": ["dom"] * 10 + [f"a{i}" for i in range(20)],
                "b": ["dom"] * 10 + [f"b{i}" for i in range(20)],
            }
        )
        snap = compute_distribution_snapshot(df, joint_columns=[("a", "b")], contingency_top_k=1)
        warnings = score_categorical_retention(snap, threshold=0.8)
        joint_warnings = [w for w in warnings if "categorical_retention_joint_collapse" in w]
        assert len(joint_warnings) == 1
        assert "columns=['a', 'b']" in joint_warnings[0]

    def test_full_joint_retention_does_not_warn(self):
        df = pd.DataFrame({"a": ["x", "x", "y"], "b": ["1", "1", "2"]})
        snap = compute_distribution_snapshot(df, joint_columns=[("a", "b")])
        assert score_categorical_retention(snap, threshold=0.8) == []

    def test_threshold_zero_silences_joint_collapse(self):
        df = pd.DataFrame(
            {
                "a": ["dom"] * 10 + [f"a{i}" for i in range(20)],
                "b": ["dom"] * 10 + [f"b{i}" for i in range(20)],
            }
        )
        snap = compute_distribution_snapshot(df, joint_columns=[("a", "b")], contingency_top_k=1)
        warnings = score_categorical_retention(snap, threshold=0.0)
        assert not [w for w in warnings if "categorical_retention_joint_collapse" in w]


class TestWarnOnlyContract:
    def test_scorer_never_mutates_snapshot(self):
        df = pd.DataFrame({"notes": [f"note number {i} with text" for i in range(50)]})
        snap = compute_distribution_snapshot(df)
        before = copy.deepcopy(snap)
        score_categorical_retention(snap, threshold=0.8)
        assert json.dumps(snap, sort_keys=True) == json.dumps(before, sort_keys=True)

    def test_logger_wrapper_logs_each_warning(self, caplog):
        import logging

        df = pd.DataFrame({"notes": [f"note number {i} with text" for i in range(50)]})
        snap = compute_distribution_snapshot(df)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.quality._retention_gate"):
            warn_on_low_categorical_retention(snap, threshold=0.8)
        messages = [r.message for r in caplog.records]
        assert len(messages) == 1
        assert "categorical_retention_cardinality_cliff" in messages[0]

    def test_logger_wrapper_silent_on_clean_snapshot(self, caplog):
        import logging

        df = pd.DataFrame({"state": ["CA", "NY", "TX"]})
        snap = compute_distribution_snapshot(df)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.quality._retention_gate"):
            warn_on_low_categorical_retention(snap, threshold=0.8)
        assert caplog.records == []


class TestThresholdConfig:
    def test_default_read_from_unvalidated_dict(self):
        assert (
            categorical_retention_warn_threshold({}) == DEFAULT_CATEGORICAL_RETENTION_WARN_THRESHOLD
        )
        assert categorical_retention_warn_threshold({"global_settings": {"seed": 1}}) == 0.8
        assert (
            categorical_retention_warn_threshold(
                {"global_settings": {"categorical_retention_warn_threshold": 0.5}}
            )
            == 0.5
        )

    def test_global_settings_model_bounds(self):
        from pydantic import ValidationError

        from decoy_engine.config._global_settings import GlobalSettings

        ok = GlobalSettings(seed=1, categorical_retention_warn_threshold=0.9)
        assert ok.categorical_retention_warn_threshold == 0.9
        assert GlobalSettings(seed=1).categorical_retention_warn_threshold == 0.8
        with pytest.raises(ValidationError):
            GlobalSettings(seed=1, categorical_retention_warn_threshold=1.5)
        with pytest.raises(ValidationError):
            GlobalSettings(seed=1, categorical_retention_warn_threshold=-0.1)

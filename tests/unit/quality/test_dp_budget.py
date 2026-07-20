"""Unit tests for decoy_engine.quality.dp_budget.PrivacyBudget (DPS-2)."""

from __future__ import annotations

import pytest

from decoy_engine.quality.dp_budget import PrivacyBudget


def test_sequential_composition_sums_epsilon():
    b = PrivacyBudget()
    b.charge("col_a.histogram", epsilon=1.0)
    b.charge("col_b.histogram", epsilon=0.5)
    assert b.total_epsilon() == pytest.approx(1.5)
    assert b.total_delta() == 0.0
    assert len(b.breakdown()) == 2


def test_rejects_nonpositive_epsilon():
    b = PrivacyBudget()
    with pytest.raises(ValueError, match="epsilon"):
        b.charge("bad", epsilon=0.0)


def test_rejects_negative_epsilon():
    b = PrivacyBudget()
    with pytest.raises(ValueError, match="epsilon"):
        b.charge("bad", epsilon=-1.0)


def test_rejects_negative_delta():
    b = PrivacyBudget()
    with pytest.raises(ValueError, match="delta"):
        b.charge("bad", epsilon=1.0, delta=-1e-6)


def test_composes_delta_and_records_mechanism():
    b = PrivacyBudget()
    b.charge("col_a.histogram", epsilon=1.0, delta=1e-6, mechanism="laplace")
    b.charge("col_b.top_values", epsilon=0.5, delta=1e-6, mechanism="laplace")
    assert b.total_delta() == pytest.approx(2e-6)
    breakdown = b.breakdown()
    assert breakdown == [
        {"label": "col_a.histogram", "epsilon": 1.0, "delta": 1e-6, "mechanism": "laplace"},
        {"label": "col_b.top_values", "epsilon": 0.5, "delta": 1e-6, "mechanism": "laplace"},
    ]


def test_empty_budget_totals_are_zero():
    b = PrivacyBudget()
    assert b.total_epsilon() == 0.0
    assert b.total_delta() == 0.0
    assert b.breakdown() == []

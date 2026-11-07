"""A9: the SDMetrics-style pairwise helper scores known cases exactly."""

from __future__ import annotations

import pandas as pd
import pytest

from ._pairwise_fidelity import contingency_similarity, correlation_similarity, pairwise_fidelity


def test_identical_frames_score_one() -> None:
    frame = pd.DataFrame(
        {"a": [1.0, 2.0, 3.0, 5.0], "b": [2.0, 1.0, 4.0, 3.0], "c": ["x", "y", "x", None]}
    )
    assert all(s.score == pytest.approx(1.0) for s in pairwise_fidelity(frame, frame))


def test_two_by_two_contingency() -> None:
    real = pd.DataFrame({"a": ["p", "p", "q", "q"], "b": ["u", "v", "u", "v"]})
    synth = pd.DataFrame({"a": ["p", "p", "p", "q"], "b": ["u", "u", "v", "v"]})
    # real cells 1/4 each; synth: pu 1/2, pv 1/4, qv 1/4 -> TVD = (1/4 + 0 + 1/4 + 0) / 2
    assert contingency_similarity(real, synth, "a", "b") == pytest.approx(0.75)


def test_nulls_are_their_own_category() -> None:
    real = pd.DataFrame({"a": ["p", None], "b": ["u", "u"]})
    synth = pd.DataFrame({"a": ["p", "p"], "b": ["u", "u"]})
    assert contingency_similarity(real, synth, "a", "b") == pytest.approx(0.5)


def test_pearson_similarity() -> None:
    real = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [1.0, 2.0, 3.0]})
    synth = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0]})
    assert correlation_similarity(real, synth, "a", "b", "pearson") == pytest.approx(0.0)


def test_constant_column_scores_none() -> None:
    real = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [1.0, 1.0, 1.0]})
    assert correlation_similarity(real, real, "a", "b", "pearson") is None


def test_mismatched_columns_raise() -> None:
    with pytest.raises(ValueError, match="same columns"):
        pairwise_fidelity(pd.DataFrame({"a": [1]}), pd.DataFrame({"b": [1]}))

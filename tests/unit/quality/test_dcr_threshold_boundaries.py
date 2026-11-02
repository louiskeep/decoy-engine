"""Boundary pins for the holdout-comparison classification in `_dcr`.

The thresholds (0.50 borderline floor, 0.85 generalizing floor) are SDV's; a
shifted cutoff changes what a customer's report says about memorization, so
each edge is asserted just below, exactly at, and just above.
"""

from __future__ import annotations

import pytest

from decoy_engine.quality._dcr import _interpret_holdout_comparison

# holdout median fixed at 1.0 so the ratio equals the source median exactly.
_BOUNDARY_CASES = [
    (0.4999, "memorizing"),
    (0.50, "borderline"),
    (0.5001, "borderline"),
    (0.8499, "borderline"),
    (0.85, "generalizing"),
    (0.8501, "generalizing"),
    (0.90, "generalizing"),
]


@pytest.mark.parametrize(("ratio", "expected"), _BOUNDARY_CASES)
def test_ratio_classification_at_thresholds(ratio: float, expected: str) -> None:
    result = _interpret_holdout_comparison(ratio, 1.0)
    assert result["interpretation"] == expected
    assert result["median_ratio"] == pytest.approx(ratio, abs=1e-4)

"""Report invariants: frozen, value-free, refuses impossible values."""

from __future__ import annotations

import dataclasses
from typing import Any

import pandas as pd
import pytest

from decoy_engine.anonymize.microaggregation import MicroaggregationReport, microaggregate


def _report(**changes: Any) -> MicroaggregationReport:
    base = microaggregate(pd.DataFrame({"x": [1.0, 2.0, 9.0, 10.0]}), ["x"], k=2).report
    return dataclasses.replace(base, **changes)


def test_valid_report_builds() -> None:
    report = _report()
    assert report.group_size_distribution == ((2, 2),)
    # Groups {1, 2} and {9, 10}; raw SSE 1, population variance 16.25, SST 4.
    assert report.sse == pytest.approx(1 / 16.25, rel=1e-12)
    assert report.information_loss == pytest.approx(1 / 65, rel=1e-12)


@pytest.mark.parametrize(
    "changes",
    [
        {"sse": float("nan")},
        {"sst": float("inf")},
        {"information_loss": 1.5},
        {"information_loss": -0.1},
        {"row_count": -1},
        {"group_count": -1},
        {"column_information_loss": (float("nan"),)},
        {"column_information_loss": (2.0,)},
        {"k": True},
        {"quasi_identifiers": ("x", 1)},
    ],
)
def test_impossible_values_refused(changes: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)) as info:
        _report(**changes)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_report_carries_no_cell_value() -> None:
    frame = pd.DataFrame({"x": [7731.25, 7731.5, 1.0, 2.0]})
    report = microaggregate(frame, ["x"], k=2).report
    assert "7731" not in repr(report)
    fields = {f.name for f in dataclasses.fields(report)}
    assert fields == {
        "row_count",
        "k",
        "quasi_identifiers",
        "group_count",
        "group_size_min",
        "group_size_max",
        "group_size_distribution",
        "sse",
        "sst",
        "information_loss",
        "column_information_loss",
    }

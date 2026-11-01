"""numpy 2.5 deprecates the generic timedelta unit and `pd.Timedelta(days=n)` hits it.

Each site below runs with that warning promoted to an error, so the build breaks the day numpy
turns the deprecation into one. The results are pinned so the replacement cannot drift.
"""

from __future__ import annotations

import warnings

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution.native._bucket_perturb_ext import native_range_exceeded
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.transforms.windowed_date import WindowedDateError, _shift_and_format


@pytest.fixture(autouse=True)
def _generic_unit_is_an_error():
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message=".*'generic' unit.*", category=DeprecationWarning)
        yield


@pytest.mark.parametrize(
    ("offset", "expected"), [(0, "2020-01-01"), (3, "2020-01-04"), (-400, "2018-11-27")]
)
def test_windowed_date_shift_uses_a_specific_timedelta_unit(offset: int, expected: str) -> None:
    assert _shift_and_format(pd.Timestamp("2020-01-01"), offset, 0) == expected


def test_windowed_date_shift_still_reports_an_out_of_range_result() -> None:
    with pytest.raises(WindowedDateError):
        _shift_and_format(pd.Timestamp("2020-01-01"), 10**9, 0)


@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
def test_bucket_range_guard_uses_a_specific_timedelta_unit(bucket: str) -> None:
    near_the_floor = pa.array(["1677-09-22"])
    assert native_range_exceeded(near_the_floor, bucket, "%Y-%m-%d") is True
    assert native_range_exceeded(pa.array(["2020-06-15"]), bucket, "%Y-%m-%d") is False


def test_days_from_now_uses_a_specific_timedelta_unit() -> None:
    cg = ColumnGenerator(seed=42, reference_date=pd.Timestamp("2026-01-01"))
    col = {"name": "y", "type": "formula", "formula": "days_from_now(30)"}
    assert set(cg.generate_column(2, col, "t", {}).tolist()) == {"2026-01-31"}


def test_days_from_now_still_fails_the_row_on_a_non_number() -> None:
    cg = ColumnGenerator(seed=42, reference_date=pd.Timestamp("2026-01-01"))
    col = {"name": "y", "type": "formula", "formula": 'days_from_now(float("nan"))'}
    assert cg.generate_column(2, col, "t", {}).tolist() == [None, None]

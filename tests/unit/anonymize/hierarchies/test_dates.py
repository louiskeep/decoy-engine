"""Date hierarchy: literal labels, typed and tz-aware inputs, mixed zones, refusals."""

from __future__ import annotations

import datetime
import warnings
import zoneinfo

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.anonymize.hierarchies import check_hierarchy, date_hierarchy

from ._cols import labels, obj

DAYS = [datetime.date(2024, 3, 5), datetime.date(2024, 11, 30), datetime.date(2023, 12, 31)]
TABLE = {
    1: ["2024-03", "2024-11", "2023-12"],
    2: ["2024Q1", "2024Q4", "2023Q4"],
    3: ["2024", "2024", "2023"],
    4: ["*", "*", "*"],
}
NY = zoneinfo.ZoneInfo("America/New_York")
UTC = datetime.timezone.utc


@pytest.mark.parametrize(
    "column",
    [
        obj(DAYS),
        pd.Series(pd.to_datetime([d.isoformat() for d in DAYS])),
        pa.array(DAYS, pa.date32()),
        pa.array([datetime.datetime(d.year, d.month, d.day, 12) for d in DAYS],
                 pa.timestamp("us", tz="UTC")),
    ],
)  # fmt: skip
def test_literal_table(column: object) -> None:
    h = date_hierarchy()
    assert (h.height, h.levels, h.kind) == (4, ("month", "quarter", "year"), "date")
    for level, expected in TABLE.items():
        assert labels(h.generalize(column, level)) == expected


def test_tz_aware_uses_own_wall_time_without_warnings() -> None:
    # 2024-04-01 03:30 UTC is 2024-03-31 23:30 in New York.
    instant = pd.Timestamp("2024-04-01 03:30", tz="UTC")
    pandas_ny = pd.Series([instant]).dt.tz_convert(NY)
    arrow_ny = pa.array([instant.to_pydatetime()], pa.timestamp("us", tz="America/New_York"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        h = date_hierarchy()
        assert labels(h.generalize(pandas_ny, 1)) == ["2024-03"]
        assert labels(h.generalize(arrow_ny, 1)) == ["2024-03"]
        assert labels(h.generalize(obj([instant.tz_convert(NY)]), 1)) == ["2024-03"]
        assert labels(h.generalize(pd.Series([instant]), 1)) == ["2024-04"]


def test_nulls_strings_and_padding() -> None:
    h = date_hierarchy()
    column = pd.Series(pd.to_datetime(["2024-03-05", None]))
    assert labels(h.generalize(column, 1)) == ["2024-03", None]
    assert labels(h.generalize(column, 4)) == ["*", "*"]
    assert check_hierarchy(h, obj(["2024-03-05", datetime.date(2024, 1, 1)])).rows_not_covered == 1
    early = obj([datetime.date(999, 5, 1)])
    assert [labels(h.generalize(early, lv))[0] for lv in (1, 2, 3)] == ["0999-05", "0999Q2", "0999"]


def test_matches_pandas_period_for_four_digit_years() -> None:
    h = date_hierarchy()
    stamps = [pd.Timestamp("1000-01-01"), pd.Timestamp("2024-08-15"), pd.Timestamp("2262-04-01")]
    for level, freq in ((1, "M"), (2, "Q"), (3, "Y")):
        expected = [str(pd.Period(t, freq)) for t in stamps]
        assert labels(h.generalize(obj(stamps), level)) == expected


def test_numpy_datetime64_in_object_column() -> None:
    h = date_hierarchy(("year",))
    column = obj([np.datetime64("2020-02-02"), np.datetime64("0001-01-01"), np.datetime64("NaT")])
    assert labels(h.generalize(column, 1)) == ["2020", "0001", None]


@pytest.mark.parametrize(
    "cells",
    [
        [datetime.datetime(2024, 4, 1, 3, 30, tzinfo=UTC),
         datetime.datetime(2024, 3, 31, 23, 30, tzinfo=NY)],
        [datetime.datetime(2024, 3, 31, 23, 30, tzinfo=NY),
         datetime.datetime(2024, 4, 1, 3, 30, tzinfo=UTC)],
        [datetime.datetime(2024, 4, 1), datetime.datetime(2024, 4, 1, tzinfo=UTC)],
        [datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))),
         datetime.datetime(2024, 7, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=-4)))],
    ],
)  # fmt: skip
def test_mixed_zones_refused_in_any_order(cells: list[object]) -> None:
    for column in (obj(cells), obj(list(reversed(cells)))):
        with pytest.raises(TypeError, match="mixed time zones"):
            date_hierarchy().generalize(column, 1)
        with pytest.raises(TypeError, match="mixed time zones"):
            check_hierarchy(date_hierarchy(), column)


def test_utc_aliases_are_one_zone() -> None:
    cells = [
        datetime.datetime(2024, 1, 1, tzinfo=UTC),
        datetime.datetime(2024, 2, 1, tzinfo=zoneinfo.ZoneInfo("UTC")),
        datetime.datetime(2024, 3, 1, tzinfo=zoneinfo.ZoneInfo("Etc/UTC")),
        pd.Timestamp("2024-04-01", tz="UTC"),
    ]
    assert labels(date_hierarchy().generalize(obj(cells), 1)) == [
        "2024-01", "2024-02", "2024-03", "2024-04"]  # fmt: skip


def test_month_year_only() -> None:
    h = date_hierarchy(("month", "year"))
    assert h.height == 3
    assert labels(h.generalize(obj(DAYS[:1]), 2)) == ["2024"]


@pytest.mark.parametrize(
    "levels",
    [(), ("week",), ("year", "month"), ("month", "month"), "month", (1,), ("Month",)],
)
def test_refusals(levels: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        date_hierarchy(levels)  # type: ignore[arg-type]

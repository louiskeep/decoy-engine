"""The date_shift handler with entity_key: one offset per entity, intervals kept, failures coded."""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine import reasons
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._date_shift import (
    DateShiftStrategyHandler,
    _overflow_positions,
)
from tests.unit.transforms.date_shift_entity._support import ctx, seed

_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S")


def _run(
    dates: list[Any], entities: list[Any], ctx_: Any = None, **cfg: Any
) -> tuple[list[Any], Any]:
    df = pd.DataFrame({"d": dates, "pid": entities})
    c = ctx_ or ctx({"pid": df["pid"].copy()})
    cfg.setdefault("entity_key", "pid")
    out, warnings = DateShiftStrategyHandler().run(df, "d", seed(**cfg), c)
    assert warnings == []
    return list(out["d"]), c


def _plain(date: str, **cfg: Any) -> Any:
    """The plain per-value shift of one date (the no-entity fallback)."""
    df = pd.DataFrame({"d": [date]})
    cfg.pop("entity_key", None)
    out, _ = DateShiftStrategyHandler().run(df, "d", seed(**cfg), ctx())
    return out["d"].iloc[0]


_ENTITY = st.one_of(
    st.sampled_from(["p1", "p2", "p3", "p4"]),
    st.integers(min_value=0, max_value=5),
    st.sampled_from([None, "", "  "]),
)


@settings(max_examples=60, deadline=None, derandomize=True)
@given(
    rows=st.lists(
        st.tuples(st.dates(dt.date(1950, 1, 1), dt.date(2100, 12, 31)), _ENTITY),
        min_size=1,
        max_size=40,
    ),
    bounds=st.tuples(st.integers(-3000, 3000), st.integers(-3000, 3000)),
    fmt=st.sampled_from(_FORMATS),
)
def test_one_offset_per_entity_and_intervals_kept(
    rows: list[tuple[dt.date, Any]], bounds: tuple[int, int], fmt: str
) -> None:
    lo, hi = sorted(bounds)
    dates = [d.strftime(fmt) for d, _ in rows]
    entities = [e for _, e in rows]
    out, c = _run(dates, entities, min_days=lo, max_days=hi, date_format=fmt)
    shifts: dict[Any, int] = {}
    no_entity = 0
    for src, got, ent in zip(dates, out, entities, strict=True):
        delta = (dt.datetime.strptime(got, fmt) - dt.datetime.strptime(src, fmt)).days
        assert lo <= delta <= hi
        if ent is None or (isinstance(ent, str) and not ent.strip()):
            no_entity += 1
            assert got == _plain(src, min_days=lo, max_days=hi, date_format=fmt)
            continue
        key = str(ent)  # int 3 and "3" are one entity
        assert shifts.setdefault(key, delta) == delta
    metrics = c.date_shift_entity_fallback_metrics()
    if no_entity:
        assert metrics == {
            "date_shift_entity_fallback": [
                {"table": "t", "column": "d", "fallback_rows": no_entity}
            ]
        }
    else:
        assert metrics == {}


def test_overflow_rows_become_format_errors_and_nat_rows_are_not_counted() -> None:
    out, c = _run(
        ["2262-04-10", "2020-01-01", None, "not a date"],
        ["p1", "p1", "p1", "p1"],
        min_days=5,
        max_days=10,
    )
    reasons_by_row = {e.row_index: e.reason for e in c.row_errors}
    assert reasons_by_row == {
        0: "shifted date is outside the supported date range",
        3: "value is not a parseable date under date_shift",
    }
    assert out[0] == "2262-04-10" and out[2] is None and out[3] == "not a date"
    assert out[1] != "2020-01-01"

    out, c = _run(["1677-09-22"], ["p1"], min_days=-10, max_days=-5)
    assert [e.reason for e in c.row_errors] == ["shifted date is outside the supported date range"]


def test_overflow_guard_reads_the_series_unit() -> None:
    parsed = pd.Series(pd.to_datetime(["2262-04-01", "2262-04-05"])).dt.as_unit("s")
    assert parsed.dt.unit == "s"
    # 2262-04-01 + 20 days leaves the nanosecond range; + 1 day does not.
    assert list(_overflow_positions(parsed, [20, 1], np.array([0, 1]))) == [0]
    assert list(_overflow_positions(parsed, [1, 1], np.array([0, 1]))) == []


def test_out_of_range_source_sentinel_is_a_format_error() -> None:
    out, c = _run(["9999-12-31"], ["p1"])
    assert out == ["9999-12-31"]
    assert [e.reason for e in c.row_errors] == ["value is not a parseable date under date_shift"]


@pytest.mark.parametrize(
    "value",
    [True, 1.5, dt.datetime(2020, 1, 1, 12, 0), pd.Timestamp("2020-01-01"), b"secret-id-77"],
)
def test_unsupported_entity_value_is_refused_value_free(value: Any) -> None:
    with pytest.raises(StrategyError) as info:
        _run(["2020-01-01"], [value])
    err = info.value
    assert err.code == reasons.runtime.DATE_SHIFT_ENTITY_KEY_UNSUPPORTED_VALUE
    assert repr(value) not in str(err) and "secret-id-77" not in str(err)
    assert err.__cause__ is None and err.__context__ is None


def test_missing_and_misaligned_snapshot_name_entity_key() -> None:
    df = pd.DataFrame({"d": ["2020-01-01"], "pid": ["p1"]})
    with pytest.raises(StrategyError) as info:
        DateShiftStrategyHandler().run(df, "d", seed(entity_key="pid"), ctx())
    assert info.value.code == reasons.runtime.DATE_SHIFT_GROUP_ANCHOR_SNAPSHOT_MISSING
    assert "entity_key" in str(info.value)
    shifted = pd.Series(["p1"], index=[7])
    with pytest.raises(StrategyError) as info:
        DateShiftStrategyHandler().run(df, "d", seed(entity_key="pid"), ctx({"pid": shifted}))
    assert info.value.code == reasons.runtime.DATE_SHIFT_GROUP_ANCHOR_SNAPSHOT_MISALIGNED
    assert "entity_key" in str(info.value)


@pytest.mark.parametrize(
    ("cfg", "code"),
    [
        (
            {"entity_key": "pid", "preserve_values": ["9999-12-31"]},
            reasons.runtime.DATE_SHIFT_ENTITY_KEY_UNSUPPORTED_COMBINATION,
        ),
        (
            {"entity_key": "pid", "group_by": "pid"},
            reasons.runtime.DATE_SHIFT_ENTITY_KEY_UNSUPPORTED_COMBINATION,
        ),
        ({"entity_key": 7}, reasons.runtime.DATE_SHIFT_INVALID_CONFIG),
        ({"entity_key": "pid", "min_days": "5"}, reasons.runtime.DATE_SHIFT_INVALID_CONFIG),
    ],
)
def test_raw_plan_faults_are_refused_before_any_shift(cfg: dict[str, Any], code: str) -> None:
    df = pd.DataFrame({"d": ["2020-01-01", "9999-12-31"], "pid": ["p1", "p1"]})
    with pytest.raises(StrategyError) as info:
        DateShiftStrategyHandler().run(df, "d", seed(**cfg), ctx({"pid": df["pid"].copy()}))
    assert info.value.code == code
    assert info.value.__cause__ is None and info.value.__context__ is None


class _NativeSeam:
    """A sequential-native seam that wrongly returns output."""

    def date_shift_column(self, *args: Any, **kwargs: Any) -> list[str]:
        return ["1999-01-01"]


def test_native_output_for_an_entity_column_is_refused() -> None:
    df = pd.DataFrame({"d": ["2020-01-01"], "pid": ["p1"]})
    c = ctx({"pid": df["pid"].copy()})
    object.__setattr__(c, "seq_native", _NativeSeam())
    with pytest.raises(StrategyError) as info:
        DateShiftStrategyHandler().run(df, "d", seed(entity_key="pid"), c)
    assert info.value.code == reasons.runtime.DATE_SHIFT_ENTITY_KEY_NOT_NATIVE
    assert "engine error" in str(info.value)


def test_hashed_key_order_does_not_matter() -> None:
    """The anchor is the pre-mask snapshot, so a key masked earlier changes nothing."""
    dates = ["2020-01-01", "2020-03-01"]
    snap = pd.Series(["p1", "p1"])
    df = pd.DataFrame({"d": dates, "pid": ["masked-a", "masked-b"]})
    out, _ = DateShiftStrategyHandler().run(df, "d", seed(entity_key="pid"), ctx({"pid": snap}))
    ref, _ = _run(dates, ["p1", "p1"])
    assert list(out["d"]) == ref

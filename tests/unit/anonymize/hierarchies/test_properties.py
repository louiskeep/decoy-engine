"""Generated checks: per-value labels, route parity, monotonicity, lattice consequence."""

from __future__ import annotations

import datetime
import math
import zoneinfo
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from decoy_engine.anonymize.hierarchies import (
    Hierarchy,
    Lattice,
    band_hierarchy,
    category_hierarchy,
    check_hierarchy,
    date_hierarchy,
    interval_hierarchy,
    prefix_hierarchy,
)

from ._cols import labels, obj

SETTINGS = settings(derandomize=True, max_examples=60, deadline=None)


@st.composite
def widths(draw: Any) -> tuple[int, ...]:
    out = [draw(st.integers(1, 20))]
    for _ in range(draw(st.integers(0, 2))):
        out.append(out[-1] * draw(st.integers(2, 4)))
    return tuple(out)


@st.composite
def cut_levels(draw: Any) -> tuple[tuple[int, ...], ...]:
    finest = sorted(draw(st.sets(st.integers(-100, 100), min_size=2, max_size=8)))
    levels = [tuple(finest)]
    while len(levels[-1]) > 1 and draw(st.booleans()):
        prev = list(levels[-1])
        drop = draw(st.sampled_from(prev))
        levels.append(tuple(c for c in prev if c != drop))
    return tuple(levels)


@st.composite
def hierarchies_and_values(draw: Any) -> tuple[Hierarchy, list[Any]]:
    kind = draw(st.sampled_from(["interval", "band", "prefix", "date", "category"]))
    nulls = st.none()
    if kind in ("interval", "band"):
        h: Hierarchy = (
            interval_hierarchy(draw(widths()))
            if kind == "interval"
            else band_hierarchy(draw(cut_levels()))
        )
        value = st.one_of(nulls, st.integers(-(10**20), 10**20),
                          st.floats(allow_nan=False, allow_infinity=False))  # fmt: skip
    elif kind == "prefix":
        h = prefix_hierarchy(4)
        value = st.one_of(nulls, st.text("abc", min_size=4, max_size=4))
    elif kind == "date":
        levels = draw(
            st.lists(st.sampled_from(["month", "quarter", "year"]), min_size=1, unique=True)
        )
        h = date_hierarchy(tuple(sorted(levels, key=["month", "quarter", "year"].index)))
        value = st.one_of(nulls, st.dates())
    else:
        size = draw(st.integers(1, 8))
        groups = draw(st.integers(1, size))
        rows = [(f"v{i}", f"g{i % groups}", f"t{(i % groups) % 2}") for i in range(size)]
        h = category_hierarchy(rows)
        value = st.one_of(nulls, st.sampled_from([r[0] for r in rows]))
    return h, draw(st.lists(value, max_size=30))


@SETTINGS
@given(hierarchies_and_values(), st.data())
def test_per_value_and_monotone(case: tuple[Hierarchy, list[Any]], data: Any) -> None:
    h, values = case
    report = check_hierarchy(h, obj(values))
    assert report.covers_column and report.monotone
    split = data.draw(st.integers(0, len(values)))
    order = data.draw(st.permutations(range(len(values))))
    for level in range(h.height + 1):
        whole = labels(h.generalize(obj(values), level))
        parts = labels(h.generalize(obj(values[:split]), level)) + labels(
            h.generalize(obj(values[split:]), level)
        )
        assert parts == whole
        shuffled = labels(h.generalize(obj([values[i] for i in order]), level))
        assert shuffled == [whole[i] for i in order]


MIXED = st.lists(st.sampled_from([1, True, 1.0, 2, 2.0, False, None, "1"]), max_size=12)


@SETTINGS
@given(MIXED, st.data())
@example([1, True], None)
@example([True, 1], None)
@example([1, 1.0], None)
@example([1.0, 1], None)
def test_mixed_families_are_order_free(values: list[Any], data: Any) -> None:
    order = (
        list(range(len(values)))[::-1]
        if data is None
        else data.draw(st.permutations(range(len(values))))
    )
    for h in (interval_hierarchy((5,)), category_hierarchy([(1, "one"), (2, "two")])):
        forward = check_hierarchy(h, obj(values))
        moved = check_hierarchy(h, obj([values[i] for i in order]))
        assert forward == moved


NY = zoneinfo.ZoneInfo("America/New_York")
ZONED = st.lists(
    st.sampled_from(
        [
            datetime.datetime(2024, 4, 1, 3, 30, tzinfo=datetime.timezone.utc),
            datetime.datetime(2024, 3, 31, 23, 30, tzinfo=NY),
            datetime.datetime(2024, 5, 1, tzinfo=datetime.timezone.utc),
            None,
        ]
    ),
    max_size=8,
)


def _outcome(h: Hierarchy, values: list[Any]) -> Any:
    try:
        return labels(h.generalize(obj(values), 1))
    except TypeError:
        return "refused"


@SETTINGS
@given(ZONED)
@example(
    [
        datetime.datetime(2024, 4, 1, 3, 30, tzinfo=datetime.timezone.utc),
        datetime.datetime(2024, 3, 31, 23, 30, tzinfo=NY),
    ]
)
def test_zones_are_order_free(values: list[Any]) -> None:
    h = date_hierarchy()
    forward = _outcome(h, values)
    backward = _outcome(h, values[::-1])
    if forward == "refused":
        assert backward == "refused"
    else:
        assert backward == forward[::-1]


@SETTINGS
@given(st.lists(st.one_of(st.none(), st.integers(-(10**15), 10**15)), max_size=30), widths())
def test_route_parity_ints(values: list[Any], ws: tuple[int, ...]) -> None:
    h = interval_hierarchy(ws)
    for level in range(1, h.height + 1):
        base = labels(h.generalize(pd.Series(values, dtype="Int64"), level))
        assert labels(h.generalize(pa.array(values, pa.int64()), level)) == base
        assert labels(h.generalize(obj(values), level)) == base


FLOATS = st.lists(st.one_of(st.just(math.nan), st.floats(-1e6, 1e6)), max_size=30)


@SETTINGS
@given(FLOATS, widths())
@example([1.5, math.nan], (5,))
def test_route_parity_floats_with_nan(values: list[float], ws: tuple[int, ...]) -> None:
    h = interval_hierarchy(ws)
    for level in range(1, h.height + 1):
        base = labels(h.generalize(pd.Series(values, dtype="float64"), level))
        assert (
            labels(h.generalize(pa.array(values, pa.float64(), from_pandas=False), level)) == base
        )
        arrow_backed = pd.Series(pa.array(values, pa.float64(), from_pandas=False),
                                 dtype=pd.ArrowDtype(pa.float64()))  # fmt: skip
        assert labels(h.generalize(arrow_backed, level)) == base
        assert all(
            (v is None) == math.isnan(x)
            for v, x in zip(base, values, strict=False)
            if level < h.height
        )


@SETTINGS
@given(
    st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=1, max_size=20), widths()
)
@example([1e300, 1e307, -1e307, -1.5e-323, float(2**53 + 2)], (3, 6))
def test_float_bounds_hold(values: list[float], ws: tuple[int, ...]) -> None:
    h = interval_hierarchy(ws)
    for level, width in enumerate(ws, start=1):
        for value, label in zip(values, labels(h.generalize(obj(values), level)), strict=False):
            low, high = (int(x) for x in label[1:-1].split(", "))
            assert low <= value < high and high - low == width and low % width == 0


def _min_class(frame: pd.DataFrame, columns: list[str]) -> int:
    return int(frame.groupby(columns, dropna=False).size().min())


@SETTINGS
@given(
    st.lists(
        st.tuples(st.one_of(st.none(), st.integers(0, 99)), st.text("ab", min_size=3, max_size=3)),
        min_size=1,
        max_size=40,
    ),
    st.data(),
)
def test_min_class_size_never_shrinks_upward(rows: list[tuple[Any, str]], data: Any) -> None:
    frame = pd.DataFrame({"age": pd.Series([r[0] for r in rows], dtype=object),
                          "zip": [r[1] for r in rows]})  # fmt: skip
    lattice = Lattice({"age": interval_hierarchy((5, 10)), "zip": prefix_hierarchy(3)})
    node = data.draw(st.sampled_from(list(lattice.nodes())))
    here = _min_class(lattice.apply(frame, node), ["age", "zip"])
    for successor in lattice.successors(node):
        assert _min_class(lattice.apply(frame, successor), ["age", "zip"]) >= here
    assert _min_class(lattice.apply(frame, lattice.top), ["age", "zip"]) == len(rows)


pytestmark = pytest.mark.filterwarnings("error")

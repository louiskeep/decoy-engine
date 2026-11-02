"""Filter ordering operators (lt, le, gt, ge) accept any same-family numeric bound.

An int or float literal against an int or float column compares as an exact
real number: a bound outside the column dtype's range selects every non-null
row or none, a fractional bound against an integer column is not truncated,
and a bound the column dtype cannot hold (2**53 + 1 against Float64, 0.1
against Float32) is not rounded. Identity operators (eq, ne, in) and explicit
keys keep the exact round-trip rule; the wrong family is still refused with a
value-free message.
"""

from __future__ import annotations

import math

import polars as pl
import pytest

from decoy_engine.subset._errors import SubsetConfigError
from decoy_engine.subset._keys import RI
from decoy_engine.subset._seed import select_seed_rows
from decoy_engine.subset._types import Predicate, SeedSpec
from tests.unit.subset.conftest import JOB_SEED

_BIG = 2**53


def _kf(values: list, dtype: pl.DataType) -> pl.DataFrame:
    return pl.DataFrame({"v": pl.Series(values, dtype=dtype)}).with_row_index(RI)


def _sel(kf: pl.DataFrame, op: str, value: object) -> set:
    spec = SeedSpec(table="t", mode="filter", predicates=(Predicate("v", op, value),))
    rows, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    return set(kf.filter(pl.col(RI).is_in(list(rows["t"])))["v"].to_list())


def _refused(kf: pl.DataFrame, op: str, value: object, *markers: str) -> None:
    spec = SeedSpec(table="t", mode="filter", predicates=(Predicate("v", op, value),))
    with pytest.raises(SubsetConfigError) as excinfo:
        select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert excinfo.value.code == "subset_seed_key_type"
    text = str(excinfo.value)
    assert "'t'" in text and "'v'" in text
    for marker in markers:
        assert marker not in text


def test_int64_lt_fractional_bound_selects_integers_below_it() -> None:
    kf = _kf([-1, 0, 1, 2, None], pl.Int64)
    assert _sel(kf, "lt", 1.5) == {-1, 0, 1}
    assert _sel(kf, "le", 1.5) == {-1, 0, 1}
    assert _sel(kf, "gt", 1.5) == {2}
    assert _sel(kf, "ge", 1.5) == {2}
    assert _sel(kf, "gt", -0.5) == {0, 1, 2}
    assert _sel(kf, "lt", -0.5) == {-1}


def test_uint8_bounds_outside_the_dtype_range_select_all_or_none() -> None:
    kf = _kf([0, 7, 255, None], pl.UInt8)
    every = {0, 7, 255}
    assert _sel(kf, "lt", 300) == every
    assert _sel(kf, "le", 300) == every
    assert _sel(kf, "gt", -1) == every
    assert _sel(kf, "ge", -1) == every
    assert _sel(kf, "gt", 300) == set()
    assert _sel(kf, "ge", 300) == set()
    assert _sel(kf, "lt", -1) == set()
    assert _sel(kf, "le", -1) == set()
    assert _sel(kf, "lt", 255.5) == every
    assert _sel(kf, "gt", 254.5) == {255}


def test_int64_bounds_beyond_the_int64_range() -> None:
    kf = _kf([-(2**63), 0, 2**63 - 1], pl.Int64)
    every = {-(2**63), 0, 2**63 - 1}
    assert _sel(kf, "lt", 2**63) == every
    assert _sel(kf, "lt", 1e19) == every
    assert _sel(kf, "gt", -1e19) == every
    assert _sel(kf, "ge", 2**63) == set()
    assert _sel(kf, "le", -(2**63) - 1) == set()


def test_int64_near_2_53_compares_exactly() -> None:
    kf = _kf([_BIG, _BIG + 1], pl.Int64)
    assert _sel(kf, "gt", _BIG + 1) == set()
    assert _sel(kf, "ge", _BIG + 1) == {_BIG + 1}
    assert _sel(kf, "lt", _BIG + 1) == {_BIG}
    assert _sel(kf, "gt", float(_BIG)) == {_BIG + 1}
    assert _sel(kf, "le", float(_BIG)) == {_BIG}


def test_float64_column_vs_int_literal_above_2_53_does_not_round() -> None:
    # 2**53 + 1 has no Float64 form; a cast would round it to 2**53.
    kf = _kf([float(_BIG), float(_BIG + 2)], pl.Float64)
    assert _sel(kf, "lt", _BIG + 1) == {float(_BIG)}
    assert _sel(kf, "le", _BIG + 1) == {float(_BIG)}
    assert _sel(kf, "gt", _BIG + 1) == {float(_BIG + 2)}
    assert _sel(kf, "ge", _BIG + 1) == {float(_BIG + 2)}
    assert _sel(kf, "lt", 2**1100) == {float(_BIG), float(_BIG + 2)}
    assert _sel(kf, "gt", -(2**1100)) == {float(_BIG), float(_BIG + 2)}
    assert _sel(kf, "gt", 2**1100) == set()


def test_float32_column_vs_float64_literal_does_not_round() -> None:
    # Float32(0.1) is slightly above the Float64 literal 0.1.
    kf = _kf([0.1, 0.2], pl.Float32)
    f01, f02 = sorted(kf["v"].to_list())
    assert _sel(kf, "lt", 0.1) == set()
    assert _sel(kf, "le", 0.1) == set()
    assert _sel(kf, "gt", 0.1) == {f01, f02}
    assert _sel(kf, "ge", 0.1) == {f01, f02}
    assert _sel(kf, "lt", 1e300) == {f01, f02}
    assert _sel(kf, "gt", 1e300) == set()
    assert _sel(kf, "gt", -1e300) == {f01, f02}


def test_float_column_vs_int_literal() -> None:
    kf = _kf([0.5, 1.0, 2.0, None], pl.Float64)
    assert _sel(kf, "lt", 1) == {0.5}
    assert _sel(kf, "le", 1) == {0.5, 1.0}
    assert _sel(kf, "gt", 1) == {2.0}
    assert _sel(kf, "ge", 1) == {1.0, 2.0}


def test_infinite_bounds_against_integer_column() -> None:
    kf = _kf([0, 5, None], pl.Int32)
    assert _sel(kf, "lt", math.inf) == {0, 5}
    assert _sel(kf, "gt", -math.inf) == {0, 5}
    assert _sel(kf, "gt", math.inf) == set()
    assert _sel(kf, "le", -math.inf) == set()


def test_nan_bound_against_integer_column_refused() -> None:
    _refused(_kf([0, 5], pl.Int32), "lt", math.nan, "nan")


@pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
def test_wrong_family_range_literals_still_refused(op: str) -> None:
    _refused(_kf([0, 1], pl.Int64), op, True, "True")
    _refused(_kf([0.0, 1.0], pl.Float64), op, False, "False")
    _refused(_kf([0, 1], pl.UInt8), op, "31337", "31337")
    _refused(_kf(["a", "b"], pl.String), op, 31337, "31337")


@pytest.mark.parametrize(
    ("op", "value"),
    [("eq", 1.5), ("ne", 1.5), ("in", (1.5,)), ("eq", 300), ("in", (300,)), ("ne", -1)],
)
def test_identity_operators_keep_the_exact_round_trip_rule(op: str, value: object) -> None:
    kf = _kf([0, 1, 2], pl.UInt8 if value in (300, (300,), -1) else pl.Int64)
    _refused(kf, op, value)


def test_float64_identity_operator_vs_unrepresentable_int_still_refused() -> None:
    _refused(_kf([float(_BIG)], pl.Float64), "eq", _BIG + 1)


def test_golden_rows_for_every_ordering_operator() -> None:
    kf = _kf([-3, -1, 0, 1, 2, 9, None], pl.Int16)
    golden = {
        ("lt", 0.5): {-3, -1, 0},
        ("le", 1): {-3, -1, 0, 1},
        ("gt", -1.0001): {-1, 0, 1, 2, 9},
        ("ge", 2): {2, 9},
        ("lt", -40000): set(),
        ("ge", -40000): {-3, -1, 0, 1, 2, 9},
        ("le", 40000.5): {-3, -1, 0, 1, 2, 9},
        ("gt", 8.999): {9},
    }
    for (op, value), expected in golden.items():
        assert _sel(kf, op, value) == expected, (op, value)


@pytest.mark.parametrize(
    ("op", "bound", "expected"),
    [("lt", 2, {0, 1}), ("le", 2, {0, 1, 2}), ("gt", 2, {3}), ("ge", 2, {2, 3})],
)
def test_range_expr_is_exact_for_a_bound_the_dtype_holds(
    op: str, bound: int, expected: set
) -> None:
    # The filter path only reaches `_range_expr` for bounds the dtype cannot
    # hold; the edge arithmetic must still be right for one it can.
    from decoy_engine.subset._seed import _range_expr

    df = pl.DataFrame({"v": pl.Series([0, 1, 2, 3], dtype=pl.Int64)})
    assert set(df.filter(_range_expr(pl.col("v"), op, bound, pl.Int64))["v"]) == expected

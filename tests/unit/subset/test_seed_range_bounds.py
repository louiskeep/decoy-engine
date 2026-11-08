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
import operator
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

import numpy as np
import polars as pl
import pytest

from decoy_engine.subset._errors import SubsetConfigError
from decoy_engine.subset._keys import RI
from decoy_engine.subset._seed import _is_real_bound, select_seed_rows
from decoy_engine.subset._types import Predicate, SeedSpec
from tests.unit.subset.conftest import JOB_SEED

_BIG = 2**53


_DEC = pl.Decimal(10, 2)
_ORD = ("lt", "le", "gt", "ge")


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


# Round 5: naive temporal bridge, non-finite Decimal refusal, Decimal bound on
# Float columns, and the Fraction oracle.

_D1, _D2 = date(2024, 1, 1), date(2024, 1, 2)
_T1, _T2 = datetime(2024, 1, 1, 12), datetime(2024, 1, 2)


@pytest.mark.parametrize(
    ("dtype", "values", "bound", "expected"),
    [
        (pl.Date, [_D1, _D2], datetime(2024, 1, 2, 0, 0), ({_D1}, {_D1, _D2}, set(), {_D2})),
        (pl.Date, [_D1, _D2], datetime(2024, 1, 1, 12, 0), ({_D1}, {_D1}, {_D2}, {_D2})),
        (pl.Datetime("us"), [_T1, _T2], _D2, ({_T1}, {_T1, _T2}, set(), {_T2})),
        (pl.Datetime("ms"), [_T1, _T2], _D2, ({_T1}, {_T1, _T2}, set(), {_T2})),
    ],
)
def test_naive_date_datetime_ranges_match_main(
    dtype: pl.DataType, values: list, bound: object, expected: tuple
) -> None:
    # Expected rows are main f5b46a2c's: a date is compared as its midnight.
    kf = _kf([*values, None], dtype)
    assert tuple(_sel(kf, op, bound) for op in _ORD) == expected


@pytest.mark.parametrize("op", _ORD)
def test_naive_temporal_bridge_keeps_refusals(op: str) -> None:
    utc = timezone.utc
    aware = [datetime(2024, 1, 1, 12, tzinfo=utc), datetime(2024, 1, 2, tzinfo=utc)]
    _refused(_kf([_D1, _D2], pl.Date), op, datetime(2024, 1, 2, tzinfo=utc), "2024")
    _refused(_kf(aware, pl.Datetime("us", "UTC")), op, _D2, "2024")
    _refused(_kf([_T1, _T2], pl.Datetime("ms")), op, datetime(2024, 1, 1, 12, 0, 0, 500), "2024")
    ms = [timedelta(milliseconds=1), timedelta(milliseconds=2)]
    _refused(_kf(ms, pl.Duration("ms")), op, timedelta(microseconds=1500), "1500")
    _refused(_kf([_T1, _T2], pl.Datetime("us")), "eq", _D2, "2024")
    _refused(_kf([_T1, _T2], pl.Datetime("us")), "in", (_D2,), "2024")
    _refused(_kf([_D1, _D2], pl.Date), "eq", datetime(2024, 1, 2), "2024")


_NON_FINITE = [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")]
_FINITE_DTYPES = [_DEC, pl.Int64, pl.Float64, pl.Float32]


@pytest.mark.parametrize("dtype", _FINITE_DTYPES)
@pytest.mark.parametrize("op", ["eq", "ne", "in", *_ORD])
@pytest.mark.parametrize("bound", _NON_FINITE, ids=str)
def test_non_finite_decimal_literal_refused(dtype: pl.DataType, op: str, bound: Decimal) -> None:
    value = (bound,) if op == "in" else bound
    assert not _is_real_bound(bound, dtype)  # refused at the range gate, not downstream
    _refused(_kf([0, 5, None], dtype), op, value, "NaN", "Infinity", "inf")


@pytest.mark.parametrize("dtype", _FINITE_DTYPES)
@pytest.mark.parametrize("bound", _NON_FINITE, ids=str)
def test_non_finite_decimal_explicit_key_refused(dtype: pl.DataType, bound: Decimal) -> None:
    kf = _kf([0, 5], dtype)
    spec = SeedSpec(table="t", mode="keys", key_columns=("v",), keys=((bound,),))
    with pytest.raises(SubsetConfigError) as excinfo:
        select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert excinfo.value.code == "subset_seed_key_type"
    assert "NaN" not in str(excinfo.value) and "Infinity" not in str(excinfo.value)


@pytest.mark.parametrize("dtype", [pl.Float64, pl.Float32])
def test_decimal_bound_on_float_column_is_exact(dtype: pl.DataType) -> None:
    # Float64 0.1 and Float32 0.1 both lie above the exact decimal 0.1.
    kf = _kf([0.1, 0.2, None], dtype)
    f01, f02 = sorted(kf["v"].drop_nulls().to_list())
    got = tuple(_sel(kf, op, Decimal("0.1")) for op in _ORD)
    assert got == (set(), set(), {f01, f02}, {f01, f02})
    # 0.5 is a dtype value: lt and le differ.
    kf = _kf([0.4, 0.5, 0.6], dtype)
    a, b, c = sorted(kf["v"].to_list())
    got = tuple(_sel(kf, op, Decimal("0.5")) for op in _ORD)
    assert got == ({a}, {a, b}, {c}, {b, c})


def _cell_value(x: object, dtype: pl.DataType) -> Fraction:
    if dtype == pl.Float32:
        return Fraction(float(np.float32(x)))
    if dtype.is_integer():
        return Fraction(int(x))  # type: ignore[call-overload]
    return Fraction(x)  # type: ignore[arg-type]


def _bound_value(bound: object, dtype: pl.DataType) -> Fraction:
    if type(bound) is float and isinstance(dtype, pl.Decimal):
        d = Decimal(repr(bound))  # round-trips at the column scale: main's typed rows
        if d.as_tuple().exponent >= -dtype.scale and abs(d) < 10 ** (dtype.precision - dtype.scale):  # type: ignore[operator]
            return Fraction(d)
    return Fraction(bound)  # type: ignore[arg-type]


# Built from strings: Decimal arithmetic rounds to 28 digits.
_TOP38 = Decimal("9" * 36 + ".99")
_NEG38 = Decimal("-" + "9" * 36 + ".99")
_GRID: dict[str, tuple[pl.DataType, list]] = {
    "Float64": (pl.Float64, [-1e300, -1.24, -1.23, -0.0, 0.0, 0.1, 0.2, 0.3, 0.5, 1.23, 1.24, 9.99, 1e300, 2.0**53, 2.0**53 + 2]),
    "Float32": (pl.Float32, [-3e38, -1.24, -1.23, -0.0, 0.0, 0.1, 0.2, 0.3, 0.5, 1.23, 1.24, 9.99, 3e38]),
    "Int64": (pl.Int64, [-(2**63), -2, -1, 0, 1, 2, 9, 10, 2**53, 2**53 + 1, 2**53 + 2, 2**63 - 1]),
    "UInt8": (pl.UInt8, [0, 1, 2, 9, 10, 100, 254, 255]),
    "Decimal(10,2)": (_DEC, [Decimal(s) for s in ("-99999999.99", "-1.24", "-1.23", "-0.01", "0.00", "0.01", "0.10", "0.30", "1.23", "1.24", "9.99", "99999999.99")]),
    "Decimal(38,2)": (pl.Decimal(38, 2), [_NEG38, Decimal("-1.24"), Decimal("-0.01"), Decimal("0.00"), Decimal("0.01"), Decimal("0.10"), Decimal("1.23"), Decimal("1.24"), Decimal("9.99"), Decimal("9" * 36 + ".98"), _TOP38]),
}  # fmt: skip
_BOUNDS = [
    *map(Decimal, ["0.01", "1.23", "-1.24", "0.005", "1.235", "-1.235", "0.1", "0.3", "-0.00"]),
    *map(Decimal, ["100000000", "-100000000", "1E+36", str(_TOP38), "0.5", "1E+2"]),
    Decimal("9" * 36 + ".985"), Decimal("-" + "9" * 36 + ".985"),
    0.1, 1.235, 9.99, 2**53 + 1, float(2**53 + 1),
]  # fmt: skip


def test_decimal_and_float_bounds_match_fraction_oracle() -> None:
    differs = 0
    for name, (dtype, values) in _GRID.items():
        kf = _kf([*values, None], dtype)
        cells = kf["v"].to_list()[:-1]
        for bound in _BOUNDS:
            b = _bound_value(bound, dtype)
            for op in _ORD:
                cmp = getattr(operator, op)
                want = {x for x in cells if cmp(_cell_value(x, dtype), b)}
                assert _sel(kf, op, bound) == want, (name, bound, op)
                differs += want != {x for x in cells if cmp(float(x), float(bound))}
    assert differs > 0  # the oracle is not the float answer


def test_float_bound_on_decimal_column_mirror() -> None:
    # Main f5b46a2c's rows: a float that round-trips at the scale is its decimal.
    d = [Decimal("9.98"), Decimal("9.99"), Decimal("10.00")]
    kf = _kf([*d, None], _DEC)
    got = tuple(_sel(kf, op, 9.99) for op in (*_ORD, "eq"))
    assert got == ({d[0]}, {d[0], d[1]}, {d[2]}, {d[1], d[2]}, {d[1]})


@pytest.mark.parametrize("op", _ORD)
def test_float_nan_bound_on_decimal_column_refused(op: str) -> None:
    _refused(_kf([Decimal("9.98"), Decimal("10.00")], _DEC), op, math.nan, "nan")


@pytest.mark.parametrize("bound", [Decimal("1.235"), Decimal("-1.235"), 1.235])
def test_decimal_column_inexact_bound_all_ops(bound: object) -> None:
    lo = {Decimal("-1.24"), Decimal("-1.23"), Decimal("1.23")}
    kf = _kf([Decimal("-1.24"), Decimal("-1.23"), Decimal("1.23"), Decimal("1.24"), None], _DEC)
    if bound == Decimal("-1.235"):
        below, above = {Decimal("-1.24")}, {Decimal("-1.23"), Decimal("1.23"), Decimal("1.24")}
    else:
        below, above = lo, {Decimal("1.24")}
    assert tuple(_sel(kf, op, bound) for op in _ORD) == (below, below, above, above)


@pytest.mark.parametrize(
    "big", [10**6, -(10**6), Decimal("1000"), 1e6, math.inf, -math.inf], ids=repr
)
def test_decimal_column_bound_beyond_precision_saturates(big: object) -> None:
    every = {Decimal("-99.99"), Decimal("0.00"), Decimal("99.99")}
    kf = _kf([*every, None], pl.Decimal(4, 2))
    below = every if big > 0 else set()  # type: ignore[operator]
    above = every - below
    assert tuple(_sel(kf, op, big) for op in _ORD) == (below, below, above, above)


def test_decimal_edge_is_exact_at_38_digits() -> None:
    lo = [_NEG38, Decimal("0.00"), Decimal("0.01"), Decimal("1.23")]
    hi = {Decimal("1.24"), Decimal("9" * 36 + ".98"), _TOP38}
    kf = _kf([*lo, *hi], pl.Decimal(38, 2))
    every = {*lo, *hi}
    edge = Decimal(10**36) - Decimal("0.01")
    assert _sel(kf, "le", edge) == every
    assert _sel(kf, "gt", edge) == set()
    assert _sel(kf, "le", _TOP38) == every and _sel(kf, "lt", _TOP38) == every - {_TOP38}
    assert _sel(kf, "le", Decimal("0.001")) == {_NEG38, Decimal("0.00")}
    assert _sel(kf, "gt", Decimal("0.001")) == every - {_NEG38, Decimal("0.00")}
    # An inexact bound one step inside the 38-digit edge: the grid literal must not round.
    near = Decimal("9" * 36 + ".985")
    assert _sel(kf, "le", near) == every - {_TOP38} and _sel(kf, "gt", near) == {_TOP38}
    fine = Decimal("1.2350000000000000000001")
    assert tuple(_sel(kf, op, fine) for op in _ORD) == (set(lo), set(lo), hi, hi)


@pytest.mark.parametrize("dtype", [pl.Int8, pl.Int64, pl.Float64])
def test_decimal_bound_on_integer_and_float_columns(dtype: pl.DataType) -> None:
    kf = _kf([1, 2, None] if dtype.is_integer() else [1.0, 1.5, 2.0, None], dtype)
    one, two = (1, 2) if dtype.is_integer() else (1.0, 2.0)
    mid = set() if dtype.is_integer() else {1.5}
    got = tuple(_sel(kf, op, Decimal("1.5")) for op in _ORD)
    assert got == ({one}, {one} | mid, {two}, mid | {two})


@pytest.mark.parametrize(
    ("dtype", "values", "bound"),
    [
        (pl.Date, [_D1, _D2], _D2),
        (pl.Datetime("us"), [_T1, _T2], _T2),
        (pl.Time, [time(1), time(2)], time(2)),
        (pl.Duration("us"), [timedelta(1), timedelta(2)], timedelta(2)),
        (pl.String, ["a", "b"], "b"),
        (pl.Categorical, ["a", "b"], "b"),
        (pl.Enum(["a", "b"]), ["a", "b"], "b"),
    ],
)
def test_unchanged_sweep_rows_characterization(
    dtype: pl.DataType, values: list, bound: object
) -> None:
    kf = _kf(values, dtype)
    lo, hi = kf["v"].to_list()
    assert tuple(_sel(kf, op, bound) for op in _ORD) == ({lo}, {lo, hi}, set(), {hi})


@pytest.mark.parametrize("op", _ORD)
def test_unchanged_sweep_refusals_characterization(op: str) -> None:
    aware = [datetime(2024, 1, 1, tzinfo=timezone.utc)]
    _refused(_kf(["a", "b"], pl.Enum(["a", "b"])), op, "zz9", "zz9")
    _refused(_kf(["a", "b"], pl.String), op, 31337, "31337")
    _refused(_kf([1, 2], pl.Int64), op, "31337", "31337")
    _refused(_kf([_D1], pl.Date), op, "2024-01-02", "2024")
    _refused(_kf(aware, pl.Datetime("us", "UTC")), op, datetime(2024, 1, 2), "2024")
    _refused(_kf([True, False], pl.Boolean), op, 1)
    _refused(_kf([0, 1], pl.Int64), op, True, "True")

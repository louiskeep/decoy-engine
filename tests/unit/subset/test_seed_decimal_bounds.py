"""Decimal columns and Decimal literals in seed filters and explicit keys.

Range operators (lt, le, gt, ge) compare a Decimal bound, or any int/float
bound against a Decimal column, as an exact real number: the bound moves to
the nearest value of the column's grid (k / 10**scale within the precision)
on the correct side, and a bound beyond the column's range selects every
non-null row or none. A NaN Decimal literal, and any literal polars cannot
build, is refused with the coded value-free error instead of escaping as a
polars panic or a raw RuntimeError. Identity operators keep the exact
round-trip rule.
"""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import polars as pl
import pytest

from decoy_engine.subset._errors import SubsetConfigError
from decoy_engine.subset._keys import RI
from decoy_engine.subset._seed import select_seed_rows
from decoy_engine.subset._types import Predicate, SeedSpec
from tests.unit.subset.conftest import JOB_SEED

_DEC = pl.Decimal(10, 2)


def _kf(values: list, dtype: pl.DataType) -> pl.DataFrame:
    return pl.DataFrame({"v": pl.Series(values, dtype=dtype)}).with_row_index(RI)


def _sel(kf: pl.DataFrame, op: str, value: object) -> set:
    spec = SeedSpec(table="t", mode="filter", predicates=(Predicate("v", op, value),))
    rows, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    return set(kf.filter(pl.col(RI).is_in(list(rows["t"])))["v"].to_list())


def _refused_spec(kf: pl.DataFrame, spec: SeedSpec, *markers: str) -> None:
    with pytest.raises(SubsetConfigError) as excinfo:
        select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert excinfo.value.code == "subset_seed_key_type"
    text = str(excinfo.value)
    assert "'t'" in text and "'v'" in text
    for marker in markers:
        assert marker not in text


def _refused(kf: pl.DataFrame, op: str, value: object, *markers: str) -> None:
    spec = SeedSpec(table="t", mode="filter", predicates=(Predicate("v", op, value),))
    _refused_spec(kf, spec, *markers)


@contextmanager
def _deadline(seconds: float) -> Iterator[None]:
    def _expired(*_: object) -> None:
        raise TimeoutError("bound arithmetic did not finish")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize("bound", [Decimal("1.005"), 1.005])
def test_decimal_column_bound_finer_than_the_scale(bound: object) -> None:
    kf = _kf([Decimal("0.99"), Decimal("1.00"), Decimal("1.01"), None], _DEC)
    assert _sel(kf, "lt", bound) == {Decimal("0.99"), Decimal("1.00")}
    assert _sel(kf, "le", bound) == {Decimal("0.99"), Decimal("1.00")}
    assert _sel(kf, "gt", bound) == {Decimal("1.01")}
    assert _sel(kf, "ge", bound) == {Decimal("1.01")}


def test_decimal_column_negative_bound_finer_than_the_scale() -> None:
    kf = _kf([Decimal("-1.01"), Decimal("-1.00"), Decimal("-0.99")], _DEC)
    assert _sel(kf, "lt", Decimal("-1.005")) == {Decimal("-1.01")}
    assert _sel(kf, "le", Decimal("-1.005")) == {Decimal("-1.01")}
    assert _sel(kf, "gt", Decimal("-1.005")) == {Decimal("-1.00"), Decimal("-0.99")}
    assert _sel(kf, "ge", Decimal("-1.005")) == {Decimal("-1.00"), Decimal("-0.99")}


@pytest.mark.parametrize("big", [1000, 1000.5, Decimal("100.00"), Decimal("1E+50")])
def test_decimal_column_bounds_beyond_the_precision(big: object) -> None:
    # Decimal(4, 2) holds -99.99 .. 99.99.
    kf = _kf([Decimal("-99.99"), Decimal("0.00"), Decimal("99.99"), None], pl.Decimal(4, 2))
    every = {Decimal("-99.99"), Decimal("0.00"), Decimal("99.99")}
    neg = -big
    assert _sel(kf, "lt", big) == every
    assert _sel(kf, "le", big) == every
    assert _sel(kf, "gt", big) == set()
    assert _sel(kf, "ge", big) == set()
    assert _sel(kf, "gt", neg) == every
    assert _sel(kf, "ge", neg) == every
    assert _sel(kf, "lt", neg) == set()
    assert _sel(kf, "le", neg) == set()


def test_decimal_column_with_38_digits_does_not_round() -> None:
    # A float or a 28-digit decimal context would round these together.
    dtype = pl.Decimal(38, 10)
    a = Decimal("1234567890123456789012345678.0000000001")
    b = Decimal("1234567890123456789012345678.0000000002")
    kf = _kf([a, b], dtype)
    mid = Decimal("1234567890123456789012345678.00000000015")
    assert _sel(kf, "lt", mid) == {a}
    assert _sel(kf, "le", mid) == {a}
    assert _sel(kf, "gt", mid) == {b}
    assert _sel(kf, "ge", mid) == {b}


def test_int_column_decimal_bound() -> None:
    kf = _kf([-1, 0, 1, 2, None], pl.Int64)
    assert _sel(kf, "lt", Decimal("1.5")) == {-1, 0, 1}
    assert _sel(kf, "ge", Decimal("1.5")) == {2}
    assert _sel(kf, "gt", Decimal("-0.5")) == {0, 1, 2}
    assert _sel(kf, "le", Decimal("1E-50")) == {-1, 0}
    assert _sel(kf, "lt", Decimal("1E+100")) == {-1, 0, 1, 2}
    assert _sel(kf, "gt", Decimal("1E+100")) == set()


def test_uint8_column_decimal_bound_outside_range() -> None:
    kf = _kf([0, 7, 255], pl.UInt8)
    assert _sel(kf, "lt", Decimal("300")) == {0, 7, 255}
    assert _sel(kf, "gt", Decimal("300")) == set()
    assert _sel(kf, "ge", Decimal("-1")) == {0, 7, 255}


def test_float_column_decimal_bound_beyond_float_range() -> None:
    inf = float("inf")
    kf = _kf([-inf, 0.5, inf], pl.Float64)
    assert _sel(kf, "lt", Decimal("1E+400")) == {-inf, 0.5}
    assert _sel(kf, "gt", Decimal("1E+400")) == {inf}
    assert _sel(kf, "gt", Decimal("-1E+400")) == {0.5, inf}


@pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
@pytest.mark.parametrize("dtype", [pl.Int32, pl.Float64, _DEC])
def test_infinite_decimal_bounds(dtype: pl.DataType, op: str) -> None:
    kf = _kf([0, 5, None], dtype)
    _refused(kf, op, Decimal("Infinity"), "Infinity")
    _refused(kf, op, Decimal("-Infinity"), "Infinity")


@pytest.mark.parametrize("op", ["lt", "le", "gt", "ge"])
def test_infinite_decimal_bound_keeps_float_infinities_exact(op: str) -> None:
    # A Decimal infinity is refused even where the column holds float infinities.
    inf = float("inf")
    kf = _kf([-inf, 0.5, inf], pl.Float64)
    _refused(kf, op, Decimal("Infinity"), "Infinity")
    _refused(kf, op, Decimal("-Infinity"), "Infinity")


@pytest.mark.parametrize("dtype", [pl.Int64, pl.Float64, _DEC])
@pytest.mark.parametrize("op", ["eq", "ne", "lt", "le", "gt", "ge"])
@pytest.mark.parametrize("nan", [Decimal("NaN"), Decimal("-NaN"), Decimal("sNaN")])
def test_nan_decimal_literal_refused_not_a_panic(dtype: pl.DataType, op: str, nan: Decimal) -> None:
    _refused(_kf([0, 1], dtype), op, nan, "NaN")


@pytest.mark.parametrize("dtype", [pl.Int64, pl.Float64, _DEC])
def test_infinite_decimal_identity_literal_refused(dtype: pl.DataType) -> None:
    _refused(_kf([0, 1], dtype), "eq", Decimal("Infinity"), "Infinity")
    _refused(_kf([0, 1], dtype), "in", (Decimal("-Infinity"),), "Infinity")


@pytest.mark.parametrize("dtype", [pl.Int64, _DEC])
def test_decimal_literal_too_large_for_polars_refused_on_identity(dtype: pl.DataType) -> None:
    _refused(_kf([0, 1], dtype), "eq", Decimal("1E+100"), "1E+100", "Decimal128")
    _refused(_kf([0, 1], dtype), "in", (Decimal("1" * 46),), "1111", "Decimal128")


@pytest.mark.parametrize(
    "key",
    [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("1E+100"), Decimal("1.005")],
)
def test_explicit_decimal_keys_refused_with_the_coded_error(key: Decimal) -> None:
    kf = _kf([Decimal("1.00"), Decimal("2.00")], _DEC)
    spec = SeedSpec(table="t", mode="keys", key_columns=("v",), keys=((key,),))
    _refused_spec(kf, spec, "NaN", "Infinity", "1E+100", "1.005", "Decimal128")


def test_explicit_decimal_key_exact_value_still_selects() -> None:
    kf = _kf([Decimal("1.00"), Decimal("2.00")], _DEC)
    spec = SeedSpec(table="t", mode="keys", key_columns=("v",), keys=((Decimal("2.00"),),))
    rows, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert rows["t"] == frozenset({1})


@pytest.mark.parametrize("exp", ["1E+999999999", "1E-999999999", "-1E+999999999"])
@pytest.mark.parametrize("dtype", [pl.Int64, pl.Float64, _DEC])
def test_huge_decimal_exponent_finishes(exp: str, dtype: pl.DataType) -> None:
    kf = _kf([-1, 0, 1], dtype)
    every = set(kf["v"].to_list())
    bound = Decimal(exp)
    with _deadline(5):
        below = _sel(kf, "lt", bound)
        above = _sel(kf, "gt", bound)
    if exp == "1E+999999999":
        assert (below, above) == (every, set())
    elif exp == "-1E+999999999":
        assert (below, above) == (set(), every)
    else:
        zero = 0 if dtype != _DEC else Decimal("0.00")
        assert below == {x for x in every if x <= zero}
        assert above == {x for x in every if x > zero}


@pytest.mark.parametrize(
    ("op", "value"), [("eq", Decimal("1.005")), ("ne", Decimal("1.005")), ("in", (1.005,))]
)
def test_decimal_identity_operators_keep_the_round_trip_rule(op: str, value: object) -> None:
    _refused(_kf([Decimal("1.00"), Decimal("1.01")], _DEC), op, value, "1.005")


def test_decimal_identity_operator_with_exact_value_selects() -> None:
    kf = _kf([Decimal("1.00"), Decimal("1.01")], _DEC)
    assert _sel(kf, "eq", Decimal("1.01")) == {Decimal("1.01")}
    assert _sel(kf, "eq", 1) == {Decimal("1.00")}


@pytest.mark.parametrize(
    ("op", "bound", "expected"),
    [
        ("lt", Decimal("1.01"), {Decimal("1.00")}),
        ("le", Decimal("1.01"), {Decimal("1.00"), Decimal("1.01")}),
        ("gt", Decimal("1.01"), {Decimal("1.02")}),
        ("ge", Decimal("1.01"), {Decimal("1.01"), Decimal("1.02")}),
    ],
)
def test_range_expr_decimal_grid_for_a_bound_the_dtype_holds(
    op: str, bound: Decimal, expected: set
) -> None:
    from decoy_engine.subset._seed import _range_expr

    df = pl.DataFrame(
        {"v": pl.Series([Decimal("1.00"), Decimal("1.01"), Decimal("1.02")], dtype=_DEC)}
    )
    assert set(df.filter(_range_expr(pl.col("v"), op, bound, _DEC))["v"]) == expected

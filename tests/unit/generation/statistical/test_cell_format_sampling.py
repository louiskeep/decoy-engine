"""Fitted cell format applied by the sampler: nulls, blanks, precision, resolution (AT-1 to AT-4)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.statistical import _cell_format as cf
from decoy_engine.generation.statistical import sample_column, spec_from_dict, spec_to_dict
from tests.unit.generation.statistical._cf_support import (
    band,
    fit,
    mixed_frame,
    none_rate,
    null_at,
    spec_of,
)

N_OUT = 20_000


# AT-1 nulls carry


@pytest.mark.parametrize(
    ("name", "rate"), [("price", 0.10), ("tier", 0.25), ("seen", 0.40), ("note", 0.50)]
)
def test_generated_null_rate_follows_the_source(name: str, rate: float) -> None:
    snap = fit(mixed_frame())
    assert snap["columns"][name]["null_count"] > 0  # non-vacuity: the source has nulls
    out = sample_column(spec_of(snap, name), N_OUT, col_seed=7)
    low, high = band(rate, N_OUT)
    assert low <= none_rate(out) <= high
    assert any(v is not None for v in out)


def test_a_snapshot_without_the_block_draws_no_fitted_nulls() -> None:
    snap = fit(mixed_frame(), cell_format=False)
    assert snap["columns"]["price"]["null_count"] > 0
    out = sample_column(spec_of(snap, "price"), 2_000, col_seed=7)
    assert none_rate(out) == 0.0
    assert "cell_format" not in spec_to_dict(spec_of(snap, "price"))


def test_a_null_probability_key_wins_over_the_fitted_rate() -> None:
    snap = fit(mixed_frame())
    for prob in (0, 0.1):
        spec = spec_of(snap, "price", null_probability=prob)
        assert spec.cell_format is not None and not spec.cell_format.apply_nulls
        assert none_rate(sample_column(spec, 2_000, col_seed=7)) == 0.0


def test_all_null_column_generates_all_nulls_with_a_block_and_is_refused_without() -> None:
    frame = pd.DataFrame({"void": [None] * 50, "x": range(50)})
    out = sample_column(spec_of(fit(frame), "void"), 25, col_seed=3)
    assert out == [None] * 25
    assert sample_column(spec_of(fit(frame), "void"), 5, col_seed=3, start=9) == [None] * 5
    with pytest.raises(Exception) as exc:
        spec_of(fit(frame, cell_format=False), "void")
    assert getattr(exc.value, "code", "") == "statistical_kind_unsupported"


def test_windowed_read_equals_the_same_rows_of_a_full_run() -> None:
    snap = fit(mixed_frame())
    for name in ("price", "tier", "seen", "note"):
        spec = spec_of(snap, name)
        full = sample_column(spec, 300, col_seed=21)
        assert sample_column(spec, 100, col_seed=21, start=150) == full[150:250]


# AT-2 blanks carry


def _blank_frame(n: int = 2_000) -> pd.DataFrame:
    values: list[Any] = [f"free text {i} " + "y" * (i % 30) for i in range(n)]
    for cell, mask in (
        ("", null_at(n, 0.30, 1)),
        (" ", null_at(n, 0.03, 2)),
        ("\t", null_at(n, 0.02, 3)),
    ):
        for i in np.flatnonzero(mask):
            values[i] = cell
    for i in np.flatnonzero(null_at(n, 0.10, 4)):
        values[i] = None
    return pd.DataFrame({"note": pd.Series(values, dtype=object)})


def test_blank_count_counts_only_empty_strings_and_the_rates_carry() -> None:
    frame = _blank_frame()
    snap = fit(frame)
    entry = snap["columns"]["note"]
    assert entry["kind"] == "freetext"
    blanks = int((frame["note"] == "").sum())
    assert blanks > 0 and entry["cell_format"]["blank_count"] == blanks
    non_null = entry["non_null_count"]
    out = sample_column(spec_of(snap, "note"), N_OUT, col_seed=5)
    low, high = band(entry["null_count"] / (entry["null_count"] + non_null), N_OUT)
    assert low <= none_rate(out) <= high
    present = [v for v in out if v is not None]
    low, high = band(blanks / non_null, len(present))
    assert low <= sum(v == "" for v in present) / len(present) <= high


def test_no_blank_appears_when_the_source_had_none() -> None:
    frame = pd.DataFrame({"note": [f"line {i} " + "z" * (i % 20) for i in range(500)]})
    snap = fit(frame)
    assert snap["columns"]["note"]["cell_format"] == {"blank_count": 0}
    out = sample_column(spec_of(snap, "note"), 5_000, col_seed=5)
    assert "" not in out


def test_a_non_blank_draw_is_never_empty_when_the_first_bin_holds_blanks() -> None:
    # Bin [0, 2) holds 6 blanks and 4 short cells; a short cell that rounds to length 0 must clamp to 1.
    spec = spec_from_dict(
        {
            "column": "c",
            "source_column": "c",
            "kind": "freetext",
            "dtype": "object",
            "stats": {"length_bin_edges": [0, 2, 3], "length_bin_counts": [10, 2]},
            "other_mode": "redistribute",
            "condition_on": None,
            "joint": None,
            "parent_first": True,
            "cell_format": {
                "null_count": 0,
                "row_count": 12,
                "apply_nulls": True,
                "decimal_places": None,
                "resolution": None,
                "blank_count": 6,
            },
        }
    )
    out = sample_column(spec, N_OUT, col_seed=9)
    low, high = band(0.5, N_OUT)
    assert low <= sum(v == "" for v in out) / N_OUT <= high


# AT-3 precision


def test_round_numeric_rounds_half_even_and_does_not_truncate() -> None:
    assert cf.round_numeric(1.239, 2) == 1.24
    assert cf.round_numeric(2.5, 0) == 2
    assert cf.round_numeric(3.5, 0) == 4


def _decimal_places(value: float) -> int:
    return max(0, -Decimal(repr(value)).as_tuple().exponent)  # type: ignore[operator]


def test_generated_floats_keep_at_most_the_source_decimal_places() -> None:
    frame = mixed_frame()
    snap = fit(frame)
    assert snap["columns"]["price"]["cell_format"] == {"decimal_places": 2}
    edges = snap["columns"]["price"]["stats"]["bin_edges"]
    out = [v for v in sample_column(spec_of(snap, "price"), 5_000, col_seed=2) if v is not None]
    assert all(isinstance(v, float) and _decimal_places(v) <= 2 for v in out)
    assert any(_decimal_places(v) == 2 for v in out)
    assert all(edges[0] <= v <= edges[-1] for v in out)


def test_a_float_column_of_whole_numbers_with_nulls_emits_int() -> None:
    rng = np.random.default_rng(3)
    frame = pd.DataFrame({"qty": rng.integers(1, 80, 400).astype("float64")})
    frame.loc[null_at(400, 0.2, 1), "qty"] = np.nan
    snap = fit(frame)
    assert snap["columns"]["qty"]["dtype"] == "float64"
    out = sample_column(spec_of(snap, "qty"), 2_000, col_seed=4)
    present = [v for v in out if v is not None]
    assert present and all(type(v) is int for v in present)
    edges = snap["columns"]["qty"]["stats"]["bin_edges"]
    low, high = edges[0], edges[-1]
    assert all(low <= v <= high for v in present)


def test_a_range_beyond_two_to_the_53_stays_float() -> None:
    rng = np.random.default_rng(3)
    frame = pd.DataFrame({"big": (rng.integers(0, 10**6, 300) * 10**11).astype("float64")})
    snap = fit(frame)
    assert snap["columns"]["big"]["cell_format"] == {"decimal_places": 0}
    edges = snap["columns"]["big"]["stats"]["bin_edges"]
    out = sample_column(spec_of(snap, "big"), 500, col_seed=4)
    assert all(type(v) is float for v in out)
    assert all(edges[0] <= v <= edges[-1] for v in out)


def test_integer_dtype_column_keeps_emitting_ints() -> None:
    frame = pd.DataFrame({"age": np.random.default_rng(1).integers(18, 90, 300)})
    out = sample_column(spec_of(fit(frame), "age"), 400, col_seed=1)
    assert all(type(v) is int and 18 <= v <= 89 for v in out)


# AT-4 resolution

_STEPS = {
    "day": "D",
    "hour": "h",
    "minute": "min",
    "second": "s",
    "millisecond": "ms",
    "microsecond": "us",
}


@pytest.mark.parametrize("unit", list(_STEPS))
def test_generated_datetimes_are_floored_to_the_source_resolution(unit: str) -> None:
    code = _STEPS[unit]
    rng = np.random.default_rng(8)
    base = pd.Timestamp("2021-03-04 05:06:07.123456")
    stamps = (base + pd.to_timedelta(rng.integers(0, 4_000, 300) * 7_919, unit="s")).floor(code)
    frame = pd.DataFrame({"seen": stamps})
    snap = fit(frame)
    assert snap["columns"]["seen"]["cell_format"] == {"resolution": unit}
    lo = datetime.fromisoformat(snap["columns"]["seen"]["stats"]["min"])
    hi = datetime.fromisoformat(snap["columns"]["seen"]["stats"]["max"])
    out = sample_column(spec_of(snap, "seen"), 2_000, col_seed=6)
    if unit == "day":
        assert all(type(v) is date for v in out)
        assert all(lo.date() <= v <= hi.date() for v in out)
        return
    assert all(type(v) is datetime and lo <= v <= hi for v in out)
    leftovers = {
        "hour": lambda d: (d.minute, d.second, d.microsecond),
        "minute": lambda d: (d.second, d.microsecond),
        "second": lambda d: (d.microsecond,),
        "millisecond": lambda d: (d.microsecond % 1000,),
        "microsecond": lambda d: (0,),
    }[unit]
    assert all(not any(leftovers(d)) for d in out)

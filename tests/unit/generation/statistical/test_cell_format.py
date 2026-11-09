"""Learn half of the fitted cell format: decimal places, resolution, blank count, member checks."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.statistical import _cell_format as cf
from decoy_engine.generation.statistical import (
    cell_format_member_ok,
    learn_cell_format,
    scrub_cell_format,
)


def _learn(values: list, kind: str, dtype: str | None = None) -> dict:
    series = pd.Series(values, dtype=dtype).dropna()
    block = learn_cell_format(series, kind)
    # Every block the learner writes must pass the same checks the decoder applies.
    assert all(cell_format_member_ok(kind, k, v) for k, v in block.items()), block
    return block


# AT-3 learn table


@pytest.mark.parametrize(
    ("values", "places"),
    [
        ([1.5, 2.25], 2),
        ([1.0, 2.0], 0),
        ([0.1 + 0.2], None),
        ([1e300], 0),
        ([-1.25, 3.5], 2),
        ([1.5, np.nan, np.inf, -np.inf], 1),
        ([1.0, np.nan, 3.0], 0),
        ([1e15 + 0.5], None),
        ([123456789012.5], 1),
        ([0.5, 0.25, 0.125], 3),
    ],
)
def test_decimal_places_table(values: list[float], places: int | None) -> None:
    assert _learn(values, "numeric") == {"decimal_places": places}


def test_integer_dtype_learns_zero() -> None:
    assert _learn([1, 2, 30], "numeric", dtype="int64") == {"decimal_places": 0}


def test_all_non_finite_numeric_learns_no_scheme() -> None:
    assert _learn([np.inf, -np.inf], "numeric") == {"decimal_places": None}


def test_tiny_column_with_17_digits_stays_inside_the_decoder_range() -> None:
    block = _learn([0.00012345678901234567, 0.00098765432109876543], "numeric")
    places = block["decimal_places"]
    assert places is None or 0 <= places <= cf.MAX_DECIMAL_PLACES


def test_the_search_never_passes_the_decoder_ceiling() -> None:
    # 0.30000000000000004 rounds to itself only at 17 places; the learner must give up at 15.
    assert cf.MAX_DECIMAL_PLACES == 15
    assert _learn([0.1 + 0.2, 0.7 + 0.1], "numeric") == {"decimal_places": None}


# AT-4 learn: one column per unit


@pytest.mark.parametrize(
    ("stamps", "unit"),
    [
        (["2021-04-26", "2021-04-27", "2023-01-02"], "day"),
        (["2021-04-26 10:00:00", "2021-04-26 13:00:00"], "hour"),
        (["2021-04-26 10:15:00", "2021-04-26 13:00:00"], "minute"),
        (["2021-04-26 10:15:07", "2021-04-26 13:00:00"], "second"),
        (["2021-04-26 10:15:07.250", "2021-04-26 13:00:00"], "millisecond"),
        (["2021-04-26 10:15:07.250001", "2021-04-26 13:00:00"], "microsecond"),
        (["2021-04-26 10:00:00.000000005"], "microsecond"),
    ],
)
def test_resolution_is_the_coarsest_unit_every_value_is_a_multiple_of(
    stamps: list[str], unit: str
) -> None:
    assert _learn(pd.to_datetime(stamps).tolist(), "datetime") == {"resolution": unit}


def test_a_tz_aware_local_midnight_column_learns_hour() -> None:
    idx = pd.date_range("2021-01-01", periods=5, freq="D", tz="America/New_York")
    assert _learn(list(idx), "datetime") == {"resolution": "hour"}


def test_resolution_ignores_rows_before_the_epoch() -> None:
    stamps = pd.to_datetime(["1951-03-04", "1969-12-31", "2001-01-01"]).tolist()
    assert _learn(stamps, "datetime") == {"resolution": "day"}


# AT-2 learn: blanks


def test_blank_count_counts_empty_strings_only() -> None:
    cells = ["", "", "", " ", "\t", *[f"free text {i}" for i in range(40)]]
    assert _learn(cells, "freetext") == {"blank_count": 3}


def test_a_freetext_column_without_blanks_writes_zero() -> None:
    assert _learn([f"free text {i}" for i in range(40)], "freetext") == {"blank_count": 0}


@pytest.mark.parametrize("kind", ["categorical", "empty"])
def test_other_kinds_carry_an_empty_block(kind: str) -> None:
    assert _learn(["a", "b"], kind) == {}


# Member descriptor (AT-8 learn-side subset)


@pytest.mark.parametrize(
    ("kind", "member", "value", "ok"),
    [
        ("numeric", "decimal_places", 0, True),
        ("numeric", "decimal_places", 15, True),
        ("numeric", "decimal_places", None, True),
        ("numeric", "decimal_places", 16, False),
        ("numeric", "decimal_places", -1, False),
        ("numeric", "decimal_places", True, False),
        ("numeric", "decimal_places", 2.0, False),
        ("numeric", "decimal_places", "2", False),
        ("datetime", "resolution", "day", True),
        ("datetime", "resolution", "week", False),
        ("datetime", "resolution", None, False),
        ("freetext", "blank_count", 0, True),
        ("freetext", "blank_count", 12, True),
        ("freetext", "blank_count", -1, False),
        ("freetext", "blank_count", True, False),
        ("freetext", "blank_count", 1.0, False),
        ("categorical", "decimal_places", 2, False),
        ("numeric", "resolution", "day", False),
        ("numeric", "mystery", 1, False),
        ("nonsense", "decimal_places", 1, False),
    ],
)
def test_member_checks_are_exact(kind: str, member: str, value: object, ok: bool) -> None:
    assert cell_format_member_ok(kind, member, value) is ok


def test_scrub_nulls_what_the_descriptor_does_not_vouch_for() -> None:
    marker = "31337.25"
    block = {"decimal_places": marker, "mystery": marker}
    out = scrub_cell_format("numeric", block)
    assert out == {"decimal_places": None, "mystery": None}
    assert marker not in json.dumps(out)
    assert scrub_cell_format("numeric", {"decimal_places": 2}) == {"decimal_places": 2}
    assert scrub_cell_format("freetext", {"decimal_places": 2}) == {"decimal_places": None}


@pytest.mark.parametrize("bad", ["31337.25", 7, [1], None])
def test_scrub_replaces_a_non_dict_block(bad: object) -> None:
    assert scrub_cell_format("numeric", bad) is None

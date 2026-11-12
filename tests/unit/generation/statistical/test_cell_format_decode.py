"""Strict reader for the fitted cell format block (AT-8): snapshot form and stored spec form."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pandas as pd
import pytest

from decoy_engine.generation.statistical import (
    StatisticalSpecError,
    sample_column,
    spec_from_dict,
    spec_to_dict,
)
from tests.unit.generation.statistical._cf_support import fit, mixed_frame, spec_of

CODE = "statistical_cell_format_invalid"
MARKER = "ZZ-MARKER-8675309"
BIG = 987654321


def _frame() -> pd.DataFrame:
    frame = mixed_frame(400)
    frame["void"] = None
    return frame


def _snap() -> dict[str, Any]:
    return fit(_frame())


def _set(column: str, block: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(snap: dict[str, Any]) -> None:
        snap["columns"][column]["cell_format"] = block

    return mutate


def _entry(column: str, **updates: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(snap: dict[str, Any]) -> None:
        snap["columns"][column].update(updates)

    return mutate


def _member(column: str, **members: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(snap: dict[str, Any]) -> None:
        snap["columns"][column]["cell_format"].update(members)

    return mutate


def _dp(snap: dict[str, Any]) -> None:
    snap["dp"] = {"epsilon": 1.0}


def _first_bin_plus_one(snap: dict[str, Any]) -> None:
    first = snap["columns"]["note"]["stats"]["length_bin_counts"][0]
    snap["columns"]["note"]["cell_format"]["blank_count"] = first + 1


def _positive_first_edge(snap: dict[str, Any]) -> None:
    snap["columns"]["note"]["stats"]["length_bin_edges"][0] = 3
    snap["columns"]["note"]["cell_format"]["blank_count"] = 1


def _row_count(snap: dict[str, Any]) -> None:
    snap["row_count"] += 1


CASES: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {
    "non_dict": ("price", _set("price", [MARKER])),
    "places_bool": ("price", _member("price", decimal_places=True)),
    "places_float": ("price", _member("price", decimal_places=2.0)),
    "places_string": ("price", _member("price", decimal_places=MARKER)),
    "places_negative": ("price", _member("price", decimal_places=-1)),
    "places_over_cap": ("price", _member("price", decimal_places=16)),
    "resolution_unknown": ("seen", _member("seen", resolution=MARKER)),
    "blank_over_first_bin": ("note", _first_bin_plus_one),
    "blank_with_positive_first_edge": ("note", _positive_first_edge),
    "unknown_member": ("price", _member("price", **{MARKER: BIG})),
    "negative_count": ("price", _entry("price", null_count=-BIG)),
    "bool_count": ("price", _entry("price", null_count=True)),
    "counts_disagree_with_row_count": ("price", _row_count),
    "places_on_categorical": ("tier", _member("tier", decimal_places=2)),
    "member_on_empty": ("void", _member("void", blank_count=BIG)),
    "block_with_dp": ("price", _dp),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_malformed_snapshot_block_is_refused_at_load(case: str) -> None:
    column, mutate = CASES[case]
    snap = _snap()
    mutate(snap)
    with pytest.raises(StatisticalSpecError) as exc:
        spec_of(snap, column)
    assert exc.value.code == CODE
    text = str(exc.value)
    assert MARKER not in text and str(BIG) not in text
    assert column in text


def test_the_unmodified_snapshot_loads() -> None:
    snap = _snap()
    for column in ("price", "tier", "seen", "note", "void"):
        assert spec_of(snap, column).cell_format is not None


def _stored(column: str) -> dict[str, Any]:
    return copy.deepcopy(spec_to_dict(spec_of(_snap(), column)))


def _stored_member(**members: Any) -> Callable[[dict[str, Any]], None]:
    return lambda d: d["cell_format"].update(members)


STORED: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {
    "non_dict": ("price", lambda d: d.update(cell_format=[MARKER])),
    "places_bool": ("price", _stored_member(decimal_places=True)),
    "places_float": ("price", _stored_member(decimal_places=2.0)),
    "places_string": ("price", _stored_member(decimal_places=MARKER)),
    "places_negative": ("price", _stored_member(decimal_places=-1)),
    "places_over_cap": ("price", _stored_member(decimal_places=16)),
    "resolution_unknown": ("seen", _stored_member(resolution=MARKER)),
    "blank_over_non_null": ("note", _stored_member(blank_count=BIG)),
    "unknown_member": ("price", _stored_member(**{MARKER: BIG})),
    "negative_count": ("price", _stored_member(null_count=-BIG)),
    "bool_count": ("price", _stored_member(row_count=True)),
    "nulls_over_rows": ("price", _stored_member(null_count=BIG)),
    "apply_nulls_not_bool": ("price", _stored_member(apply_nulls=1)),
    "missing_member": ("price", lambda d: d["cell_format"].pop("apply_nulls")),
    "places_on_categorical": ("tier", _stored_member(decimal_places=2)),
    "resolution_on_numeric": ("price", _stored_member(resolution="day")),
}


@pytest.mark.parametrize("case", sorted(STORED))
def test_a_malformed_stored_spec_is_refused_on_plan_load(case: str) -> None:
    column, mutate = STORED[case]
    data = _stored(column)
    mutate(data)
    with pytest.raises(StatisticalSpecError) as exc:
        spec_from_dict(data)
    assert exc.value.code == CODE
    text = str(exc.value)
    assert MARKER not in text and str(BIG) not in text


def test_a_stored_spec_round_trips() -> None:
    for column in ("price", "tier", "seen", "note", "void"):
        spec = spec_of(_snap(), column)
        assert spec_from_dict(spec_to_dict(spec)) == spec


def test_a_freetext_block_with_no_non_null_cells_samples_or_is_refused() -> None:
    # Untrusted producer: every cell null yet the block is present. A raw ZeroDivisionError at
    # draw time would escape the typed refusal path.
    snap = _snap()
    entry = snap["columns"]["note"]
    entry.update(null_count=entry["null_count"] + entry["non_null_count"], non_null_count=0)
    entry["cell_format"]["blank_count"] = 0
    try:
        spec = spec_of(snap, "note")
    except StatisticalSpecError as exc:
        assert exc.code == CODE
        return
    assert sample_column(spec, 20, col_seed=7) == [None] * 20

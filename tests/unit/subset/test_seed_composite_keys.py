"""Explicit composite seed keys keep tuple alignment when parts are null.

Null-key rule (same as `execution/_fk_keys.py`: a source key tuple with any
component that `fk_key_value` folds to `NULL_FK_KEY` gets no join key, so it
never matches): a requested tuple with a null part matches no source row,
including a source row whose same part is null. It is not refused; the other
tuples in the request still select their rows.
"""

from __future__ import annotations

import itertools

import polars as pl
import pytest

from decoy_engine.subset._keys import RI
from decoy_engine.subset._seed import select_seed_rows
from decoy_engine.subset._types import SeedSpec
from tests.unit.subset.conftest import JOB_SEED


def _rows(kf: pl.DataFrame, keys: tuple[tuple, ...]) -> frozenset[int]:
    spec = SeedSpec(table="t", mode="keys", key_columns=tuple(kf.columns[1:]), keys=keys)
    rows, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    return rows["t"]


def _kf(**cols: pl.Series) -> pl.DataFrame:
    return pl.DataFrame(cols).with_row_index(RI)


def test_reproduction_null_part_does_not_realign_another_tuple() -> None:
    # Moving nulls to the front of each column independently built (1, "x"),
    # (2, None) into (1, None), (2, "x") and selected row 1, never requested.
    kf = _kf(a=pl.Series([1, 2], dtype=pl.Int64), b=pl.Series([None, "x"], dtype=pl.String))
    assert _rows(kf, ((1, "x"), (2, None))) == frozenset()


def _full_grid(width: int) -> tuple[pl.DataFrame, list[tuple]]:
    """Every combination of {value, null} per column, plus a fully valued twin.

    Columns are int, string, float (mixed families), so a realignment that
    crosses columns shows up as a selected row the request never named.
    """
    values = (7, "q", 2.5)[:width]
    dtypes = (pl.Int64, pl.String, pl.Float64)[:width]
    rows = [
        tuple(v if keep else None for v, keep in zip(values, mask, strict=True))
        for mask in itertools.product((True, False), repeat=width)
    ]
    other = (8, "r", 3.5)[:width]
    rows.append(other)
    cols = {f"c{i}": pl.Series([r[i] for r in rows], dtype=dtypes[i]) for i in range(width)}
    return _kf(**cols), rows


@pytest.mark.parametrize("width", [2, 3])
def test_null_in_every_position_matches_nothing_and_keeps_alignment(width: int) -> None:
    kf, rows = _full_grid(width)
    full_row = rows.index(rows[0])  # every part valued
    other_row = len(rows) - 1
    for null_pos in range(width):
        null_key = tuple(None if i == null_pos else v for i, v in enumerate(rows[0]))
        # The null-part tuple matches nothing, not even the source row with
        # the same null part; the valued tuple still selects exactly its row.
        assert _rows(kf, (null_key,)) == frozenset()
        assert _rows(kf, (rows[other_row], null_key)) == frozenset({other_row})
        assert _rows(kf, (null_key, rows[0])) == frozenset({full_row})


@pytest.mark.parametrize("width", [2, 3])
def test_all_null_tuple_matches_nothing(width: int) -> None:
    kf, rows = _full_grid(width)
    all_null = (None,) * width
    assert _rows(kf, (all_null,)) == frozenset()
    assert _rows(kf, (all_null, rows[-1])) == frozenset({len(rows) - 1})


@pytest.mark.parametrize("width", [2, 3])
def test_every_order_of_a_mixed_request_selects_the_same_rows(width: int) -> None:
    kf, rows = _full_grid(width)
    request = [
        rows[0],
        rows[-1],
        tuple(None if i == width - 1 else v for i, v in enumerate(rows[-1])),
        tuple(None if i == 0 else v for i, v in enumerate(rows[0])),
    ]
    expected = frozenset({0, len(rows) - 1})
    for perm in itertools.permutations(request):
        assert _rows(kf, tuple(perm)) == expected


def test_crossed_null_parts_never_combine_into_a_requested_row() -> None:
    # Each requested tuple carries exactly one valued part that, paired with
    # the other tuple's valued part, would name source row 0.
    kf = _kf(a=pl.Series([5, None, 5], dtype=pl.Int64), b=pl.Series(["k", "k", None]))
    assert _rows(kf, ((5, None), (None, "k"))) == frozenset()
    assert _rows(kf, ((None, "k"), (5, None))) == frozenset()


def test_empty_in_list_still_selects_nothing() -> None:
    from decoy_engine.subset._types import Predicate

    kf = _kf(id=pl.Series([1, 2], dtype=pl.Int64))
    spec = SeedSpec(table="t", mode="filter", predicates=(Predicate("id", "in", ()),))
    rows, _, _ = select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    assert rows["t"] == frozenset()

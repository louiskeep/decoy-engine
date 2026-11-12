"""Pin the k-anonymity cases that differ from the old per-combo groupby."""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine.profiling._k_anonymity import _compute_k_anonymity, _min_group_size
from decoy_engine.profiling.types import FieldStats


def _field(df: pd.DataFrame, name: str, inferred: str) -> FieldStats:
    col = df[name]
    distinct = int(col.nunique())
    return FieldStats(
        name=name,
        inferred_type=inferred,
        dtype_raw=str(col.dtype),
        row_count=len(df),
        null_count=int(col.isna().sum()),
        null_rate=0.0,
        distinct_count=distinct,
        unique_rate=distinct / len(df),
        is_likely_unique=False,
    )


def test_unused_categories_do_not_give_k_zero() -> None:
    """An unused category is not a group; the old observed=False groupby counted it as k=0."""
    cat = pd.Categorical(["a", "b"] * 5 + ["a"], categories=["a", "b", "unused"])
    df = pd.DataFrame({"c": cat, "d": ["x"] * 10 + ["y"]})
    fields = [_field(df, "c", "string"), _field(df, "d", "string")]
    k, groups = _compute_k_anonymity(df, fields)
    assert k == 1
    assert groups == [["c", "d"]]


def test_mixed_object_combo_is_reported() -> None:
    """Ints and strings in one object column still form groups (the old groupby skipped it)."""
    df = pd.DataFrame(
        {
            "m": [1, "1", 2, "2", 1, "1", 2, "2", 3, "3"],
            "s": ["a", "a", "b", "b", "a", "a", "b", "b", "c", "c"],
        }
    )
    fields = [_field(df, "m", "mixed"), _field(df, "s", "string")]
    k, groups = _compute_k_anonymity(df, fields)
    assert k == 1
    assert groups == [["m", "s"]]


def test_min_group_size_counts_and_skips_nulls() -> None:
    """Counts rows per joint key, ignores rows with a -1 (null) code, and takes the minimum."""
    a = (pd.Series([0, 0, 0, 1, -1]).to_numpy(), 2)
    b = (pd.Series([0, 0, 0, 0, 0]).to_numpy(), 1)
    assert _min_group_size([a, b]) == 1
    all_null = (pd.Series([-1, -1]).to_numpy(), 1)
    assert _min_group_size([all_null, all_null]) is None


def _codes(values: list[int], card: int) -> tuple[np.ndarray, int]:
    return np.array(values, dtype=np.int64), card


def test_min_group_size_keys_do_not_collide() -> None:
    """(0,1) and (1,0) are different groups; a wrong radix merges them into one."""
    a = _codes([0, 1, 0, 1], 2)
    b = _codes([1, 0, 1, 0], 2)
    assert _min_group_size([a, b]) == 2
    c = _codes([0, 1, 1, 0], 2)
    assert _min_group_size([a, b, c]) == 1


def test_min_group_size_null_in_any_column_drops_the_row() -> None:
    """A -1 in any column drops the row; a dropped row must not form its own group."""
    zeros = _codes([0, 0, 0, 0, 0], 1)
    nulls_first = _codes([-1, -1, 0, 0, 0], 2)
    assert _min_group_size([nulls_first, zeros]) == 3
    assert _min_group_size([zeros, nulls_first]) == 3
    assert _min_group_size([zeros, zeros, nulls_first]) == 3
    # The null row also must not rescue a column whose other rows are non-null.
    assert _min_group_size([_codes([-1, 0, 0], 1), _codes([0, 0, 0], 1)]) == 2


def test_min_group_size_counts_each_row_once() -> None:
    a = _codes([0, 0, 0, 1, 1, 1, 1], 2)
    b = _codes([0, 0, 0, 0, 0, 0, 0], 1)
    assert _min_group_size([a, b]) == 3


def test_min_group_size_survives_radix_overflow() -> None:
    """Cardinalities whose product exceeds int64 are compacted, not wrapped."""
    big = 1 << 40
    a = _codes([0, big - 1, 0, big - 1, 5], big)
    b = _codes([big - 1, 0, big - 1, 0, 5], big)
    c = _codes([7, 7, 7, 7, 9], big)
    assert _min_group_size([a, b, c]) == 1
    assert _min_group_size([a, b]) == 1
    a2 = _codes([0, big - 1, 0, big - 1], big)
    b2 = _codes([big - 1, 0, big - 1, 0], big)
    c2 = _codes([1, 2, 1, 2], big)
    assert _min_group_size([a2, b2, c2]) == 2


def test_min_group_size_radix_is_exact() -> None:
    """Different (a, b) pairs never share a key, including when a / n would collide."""
    assert _min_group_size([_codes([2, 0], 3), _codes([0, 1], 2)]) == 1


def test_min_group_size_wide_cardinality_stays_exact() -> None:
    """Keys past 2**53 stay integers; a float key would merge these two rows."""
    wide = 1 << 60
    a = _codes([wide - 1, wide - 2], wide)
    assert _min_group_size([a, _codes([0, 0], 2)]) == 1


def test_min_group_size_unchecked_overflow_would_collide() -> None:
    """(0,0,0) and (2**23,0,0) wrap to the same int64 key unless the key is compacted."""
    big = 1 << 40
    a = _codes([0, 0, 1 << 23, 1 << 23], big)
    b = _codes([0, 0, 0, 0], 2)
    c = _codes([0, 0, 0, 0], big)
    assert _min_group_size([a, b, c]) == 2


def test_min_group_size_running_cardinality_tracks_the_product() -> None:
    """After two wide columns the running key is near 2**60, so the third must compact it."""
    w = 1 << 30
    a = _codes([0, 0, 16, 16], w)
    b = _codes([0, 0, 0, 0], w)
    c = _codes([0, 0, 0, 0], w)
    assert _min_group_size([a, b, c]) == 2


def _frame(rows: int, **cols: list[object]) -> tuple[pd.DataFrame, list[FieldStats]]:
    df = pd.DataFrame({k: v * (rows // len(v)) for k, v in cols.items()})
    return df, [_field(df, k, "string") for k in cols]


def test_tied_combos_are_all_listed_in_candidate_order() -> None:
    df, fields = _frame(12, a=["x", "y", "z"], b=["p", "q", "r", "s"], c=["m", "n"])
    # Candidates rank b (4 distinct), a (3), c (2). (b, a) is already unique per row, so the
    # pair and the triple both tie at k=1; the pair comes first because size 2 is swept first.
    k, groups = _compute_k_anonymity(df, fields)
    assert k == 1
    assert groups == [["b", "a"], ["b", "a", "c"]]


def test_fewer_than_two_rows_or_candidates_gives_none() -> None:
    df = pd.DataFrame({"a": ["x"], "b": ["y"]})
    assert _compute_k_anonymity(df, [_field(df, "a", "string"), _field(df, "b", "string")]) == (
        None,
        [],
    )
    df2, fields2 = _frame(12, a=["x", "y", "z"], b=["p"])
    assert _compute_k_anonymity(df2, fields2) == (None, [])


def test_float_and_constant_columns_are_not_candidates() -> None:
    df = pd.DataFrame({"f": [0.5, 1.5, 0.5, 1.5] * 3, "g": ["x", "y", "z", "w"] * 3})
    fields = [_field(df, "f", "float"), _field(df, "g", "string")]
    assert _compute_k_anonymity(df, fields) == (None, [])


def test_three_column_combo_can_be_the_minimum() -> None:
    df = pd.DataFrame(
        {
            "a": ["x", "x", "y", "y"] * 3,
            "b": ["p", "q", "p", "q"] * 3,
            "c": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
        }
    )
    fields = [_field(df, "a", "string"), _field(df, "b", "string"), _field(df, "c", "integer")]
    k, groups = _compute_k_anonymity(df, fields)
    assert k == 1
    assert groups == [["c", "a", "b"]]


def test_unhashable_column_is_skipped() -> None:
    df = pd.DataFrame(
        {
            "a": ["x", "y", "z", "w"] * 3,
            "b": ["p", "q"] * 6,
            "bad": [[1], [2], [3], [4]] * 3,
        }
    )
    bad = FieldStats(
        name="bad",
        inferred_type="mixed",
        dtype_raw="object",
        row_count=12,
        null_count=0,
        null_rate=0.0,
        distinct_count=4,
        unique_rate=0.33,
        is_likely_unique=False,
    )
    fields = [_field(df, "a", "string"), _field(df, "b", "string"), bad]
    k, groups = _compute_k_anonymity(df, fields)
    assert k == 3
    assert groups == [["a", "b"]]


def _stats(name: str, distinct: int, unique_rate: float, kind: str = "string") -> FieldStats:
    return FieldStats(
        name=name,
        inferred_type=kind,
        dtype_raw="object",
        row_count=0,
        null_count=0,
        null_rate=0.0,
        distinct_count=distinct,
        unique_rate=unique_rate,
        is_likely_unique=False,
    )


def test_ineligible_field_before_candidates_does_not_stop_the_scan() -> None:
    df = pd.DataFrame(
        {"f": [0.5, 1.5, 0.5, 1.5] * 3, "a": ["x", "y", "z", "w"] * 3, "b": ["p", "q"] * 6}
    )
    fields = [_field(df, "f", "float"), _field(df, "a", "string"), _field(df, "b", "string")]
    assert _compute_k_anonymity(df, fields) == (3, [["a", "b"]])


def test_unique_rate_bounds_are_exclusive() -> None:
    df = pd.DataFrame({"a": ["x", "y", "z", "w"] * 3, "b": ["p", "q"] * 6})
    at_floor = _stats("b", 2, 0.005)
    at_ceiling = _stats("b", 2, 0.95)
    inside = _stats("a", 4, 0.33)
    assert _compute_k_anonymity(df, [inside, at_floor]) == (None, [])
    assert _compute_k_anonymity(df, [inside, at_ceiling]) == (None, [])


def test_distinct_count_at_the_absolute_cap_is_still_a_candidate() -> None:
    n = 40
    df = pd.DataFrame({"a": [i % 20 for i in range(n)], "b": [i % 2 for i in range(n)]})
    a = _stats("a", 20, 0.5, "integer")
    b = _stats("b", 2, 0.05, "integer")
    assert _compute_k_anonymity(df, [a, b]) == (2, [["a", "b"]])
    over = _stats("a", 21, 0.5, "integer")
    assert _compute_k_anonymity(df, [over, b]) == (None, [])


def test_all_candidates_unhashable_gives_empty_groups() -> None:
    df = pd.DataFrame({"a": [[1], [2]] * 6, "b": [[3], [4]] * 6})
    fields = [_stats("a", 2, 0.2, "mixed"), _stats("b", 2, 0.2, "mixed")]
    assert _compute_k_anonymity(df, fields) == (None, [])


def test_combo_with_no_shared_non_null_rows_does_not_hide_later_combos() -> None:
    n = 24
    x = [f"x{i % 4}" if i < 12 else None for i in range(n)]
    y = [f"y{i % 3}" if i >= 12 else None for i in range(n)]
    z = [f"z{i % 2}" for i in range(n)]
    df = pd.DataFrame({"x": x, "y": y, "z": z})
    fields = [_stats("x", 4, 0.17), _stats("y", 3, 0.13), _stats("z", 2, 0.08)]
    # (x, y) share no row and yield no group; (x, z) gives k=3 and (y, z) gives k=2.
    assert _compute_k_anonymity(df, fields) == (2, [["y", "z"]])

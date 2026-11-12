"""GEN-REL AT-11c: the one typed lookup from a child's foreign key to its parent's row.

`lookup_parent_values` keeps three outcomes apart: a null key gives a null with no lookup, a key
that resolves to a parent whose selected value is null stays a present null, and a non-null key
with no parent is a coded refusal. List and dict keys are matched by equality, a parent key
that occurs twice is refused, and the selected column's Arrow type is kept. Every refusal is the
registered summary plus the child table and column names, raised outside any `except` block.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pyarrow as pa
import pytest
from decoy_engine.generation.relational._parent_lookup import (
    ParentLookupResult,
    lookup_parent_values,
)

from decoy_engine import reasons
from decoy_engine.generation.pool import GenerationError
from tests.unit.generation.relational._support import is_safe

SENTINEL = "SENTINELKEY"
MISSING = "parent_lookup_key_missing"
NOT_UNIQUE = "parent_lookup_key_not_unique"


def _pools(keys: list[Any], values: list[Any], value_type: pa.DataType | None = None) -> dict:
    return {"parents": pa.table({"id": pa.array(keys), "born": pa.array(values, type=value_type)})}


def _lookup(pools: dict[str, pa.Table], foreign_keys: list[Any]) -> ParentLookupResult:
    return lookup_parent_values(
        pools,
        parent_table="parents",
        key_column="id",
        value_column="born",
        foreign_keys=foreign_keys,
        child_table="kids",
        child_column="pid",
    )


def _refusal(pools: dict[str, pa.Table], foreign_keys: list[Any]) -> GenerationError:
    with pytest.raises(GenerationError) as exc:
        _lookup(pools, foreign_keys)
    return exc.value


def _text(code: str) -> str:
    return f"[{code}] {reasons.get('runtime', code).summary} Table 'kids', column 'pid'."


# ---- aligned values, one per foreign key --------------------------------------------------------


def test_each_foreign_key_gets_its_own_parents_value() -> None:
    got = _lookup(_pools([1, 2, 3], ["a", "b", "c"]), [3, 1, 1, 2])
    assert got.values == ["c", "a", "a", "b"]
    assert got.found == [True, True, True, True]
    assert got.dtype == pa.string()


def test_no_foreign_keys_give_no_values() -> None:
    got = _lookup(_pools([1], ["a"]), [])
    assert (got.values, got.found, got.dtype) == ([], [], pa.string())


@pytest.mark.parametrize(
    "keys",
    [
        [[1, 2], [3], [1, 2, 3]],
        [{"a": 1, "b": 2}, {"a": 2, "b": 1}, {"a": 1, "b": 3}],
    ],
    ids=["list", "dict"],
)
def test_list_and_dict_keys_are_matched_by_equality(keys: list[Any]) -> None:
    got = _lookup(_pools(keys, ["x", "y", "z"]), [keys[2], keys[0], keys[1]])
    assert got.values == ["z", "x", "y"]


def test_a_dict_key_matches_whatever_its_item_order() -> None:
    pools = _pools([{"a": 1, "b": 2}, {"a": 3, "b": 4}], ["x", "y"])
    assert _lookup(pools, [{"b": 4, "a": 3}]).values == ["y"]


# ---- null, present null and missing are three different things ---------------------------------


def test_a_null_foreign_key_gives_a_null_without_a_lookup() -> None:
    got = _lookup(_pools([1, 2], ["a", "b"]), [None, 2, None])
    assert got.values == [None, "b", None]
    assert got.found == [False, True, False]


def test_a_null_foreign_key_is_not_a_missing_key_even_with_no_parents() -> None:
    got = _lookup(_pools([], [], pa.string()), [None, None])
    assert (got.values, got.found) == ([None, None], [False, False])


def test_a_present_parent_with_a_null_value_stays_a_present_null() -> None:
    got = _lookup(_pools([1, 2], ["a", None]), [2, None, 1])
    assert got.values == [None, None, "a"]
    assert got.found == [True, False, True]


def test_a_non_null_key_with_no_parent_is_refused() -> None:
    exc = _refusal(_pools([1, 2], ["a", "b"]), [1, 7919])
    assert exc.code == reasons.runtime.PARENT_LOOKUP_KEY_MISSING
    assert exc.code.namespace == "runtime"
    assert str(exc) == _text(MISSING)
    assert is_safe(exc)


def test_a_missing_key_is_refused_even_when_every_value_is_null() -> None:
    """Missing and present-null must not collapse into one null."""
    exc = _refusal(_pools([1, 2], [None, None], pa.string()), [3])
    assert exc.code == reasons.runtime.PARENT_LOOKUP_KEY_MISSING


def test_a_null_parent_key_is_never_a_match() -> None:
    pools = _pools([1, None], ["a", "b"])
    assert _lookup(pools, [1, None]).values == ["a", None]
    assert _refusal(pools, [2]).code == reasons.runtime.PARENT_LOOKUP_KEY_MISSING


@pytest.mark.parametrize(
    "keys",
    [[1, 2, 1], [[1, 2], [3], [1, 2]], [{"a": 1, "b": 2}, {"b": 2, "a": 1}]],
    ids=["scalar", "list", "dict"],
)
def test_a_parent_key_that_occurs_twice_is_refused(keys: list[Any]) -> None:
    exc = _refusal(_pools(keys, [str(i) for i in range(len(keys))]), [keys[0]])
    assert exc.code == reasons.runtime.PARENT_LOOKUP_KEY_NOT_UNIQUE
    assert str(exc) == _text(NOT_UNIQUE)
    assert is_safe(exc)


def test_a_duplicate_parent_key_is_refused_even_when_no_child_asks_for_it() -> None:
    exc = _refusal(_pools([1, 2, 2], ["a", "b", "c"]), [1, None])
    assert exc.code == reasons.runtime.PARENT_LOOKUP_KEY_NOT_UNIQUE


# ---- the selected column's Arrow type ----------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "dtype"),
    [
        (["1990-01-10", None], pa.string()),
        ([dt.date(1990, 1, 10), None], pa.date32()),
        ([dt.datetime(1990, 1, 10, 3, 4, 5), None], pa.timestamp("us")),
        ([10, None], pa.int64()),
        ([1.5, None], pa.float64()),
        ([None, None], pa.string()),
    ],
    ids=["string", "date32", "timestamp", "int64", "float64", "all_null_string"],
)
def test_the_parent_columns_arrow_type_is_kept(values: list[Any], dtype: pa.DataType) -> None:
    got = _lookup(_pools([1, 2], values, dtype), [1, 2, None])
    assert got.dtype == dtype
    assert got.values == [values[0], None, None]
    assert pa.array(got.values, type=got.dtype).type == dtype


# ---- value-free refusals -----------------------------------------------------------------------


def test_refusals_name_the_child_table_and_column_only() -> None:
    keys = [f"{SENTINEL}-a", f"{SENTINEL}-b"]
    missing = _refusal(_pools(keys, ["v7919", "v7907"]), [f"{SENTINEL}-zz"])
    duplicate = _refusal(_pools([keys[0], keys[0]], ["v7919", "v7907"]), [keys[0]])
    for exc in (missing, duplicate):
        text = str(exc)
        assert SENTINEL not in text and "79" not in text
        assert not any(ch.isdigit() for ch in text)
        assert "parents" not in text and "born" not in text
        assert is_safe(exc)


def test_a_key_with_no_usable_identity_is_refused_as_missing_without_a_cause() -> None:
    """A key `value_key` cannot hash is no parent's key; the `TypeError` must not be chained."""
    exc = _refusal(_pools([1], ["a"]), [{SENTINEL: {7919}}])
    assert exc.code == reasons.runtime.PARENT_LOOKUP_KEY_MISSING
    assert SENTINEL not in str(exc) and is_safe(exc)


# ---- the contract GEN-INHERIT builds on --------------------------------------------------------


def test_a_copying_caller_gets_the_null_and_the_type_and_reuses_the_missing_key_code() -> None:
    """`inherit` copies the parent cell: a present null is copied as a typed null, an absent
    non-null key is the same registered refusal the anchored date gets."""
    pools = _pools([1, 2], [dt.date(2001, 2, 3), None], pa.date32())
    got = _lookup(pools, [2, 1, None])
    copied = pa.array(got.values, type=got.dtype)
    assert copied.type == pa.date32()
    assert copied.to_pylist() == [None, dt.date(2001, 2, 3), None]
    assert got.found == [True, True, False]
    assert _refusal(pools, [3]).code == "parent_lookup_key_missing"

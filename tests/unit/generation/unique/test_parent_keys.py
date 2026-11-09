"""GEN-UNIQUE E1: `parent_keys` is `_reference`'s parent key list, extracted unchanged (U8)."""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.generation._synthesize_derived import _reference, parent_keys


def pools(values):
    return {"p": pa.table({"id": values})}


def test_distinct_non_null_in_first_seen_order():
    got = parent_keys(pools([3, None, 1, 3, 2, None, 1]), "p", "id")
    assert got == [3, 1, 2]


def test_all_null_parent_gives_empty_list():
    assert parent_keys(pools([None, None]), "p", "id") == []


def test_strings_keep_first_seen_order():
    assert parent_keys(pools(["b", "a", "b"]), "p", "id") == ["b", "a"]


def test_reference_sequential_reads_the_same_list():
    # The sequential distribution indexes `parent_keys` directly, so its output pins the order.
    col = {
        "name": "c",
        "type": "reference",
        "reference_table": "p",
        "reference_column": "id",
        "distribution": "sequential",
        "_table": "child",
    }
    p = pools([3, None, 1, 3, 2])
    assert _reference(col, 5, 7, None, p) == [3, 1, 2, 3, 1]
    assert _reference(col, 5, 7, None, p)[:3] == parent_keys(p, "p", "id")


def test_reference_to_all_null_parent_still_gives_nones():
    col = {"name": "c", "type": "reference", "reference_table": "p", "reference_column": "id"}
    assert _reference(col, 3, 7, None, pools([None])) == [None, None, None]

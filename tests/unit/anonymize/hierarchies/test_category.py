"""Category hierarchy: literal labels, typed keys, tree refusals."""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.anonymize.hierarchies import category_hierarchy, check_hierarchy

from ._cols import labels, obj

DISEASES = [
    ("flu", "respiratory", "infectious"),
    ("pneumonia", "respiratory", "infectious"),
    ("gastritis", "digestive", "non-infectious"),
    ("ulcer", "digestive", "non-infectious"),
    ("measles", "skin", "infectious"),
]


def test_literal_labels() -> None:
    h = category_hierarchy(DISEASES)
    assert (h.height, h.kind, h.rows) == (3, "category", tuple(DISEASES))
    column = obj(["flu", "ulcer", None, "measles"])
    assert labels(h.generalize(column, 1)) == ["respiratory", "digestive", None, "skin"]
    assert labels(h.generalize(column, 2)) == ["infectious", "non-infectious", None, "infectious"]
    assert labels(h.generalize(column, 3)) == ["*"] * 4
    arrow = pa.array(["flu", "ulcer", None, "measles"]).dictionary_encode()
    assert labels(h.generalize(arrow, 2)) == ["infectious", "non-infectious", None, "infectious"]


def test_int_keys_match_by_type_family() -> None:
    h = category_hierarchy([(1, "low"), (2, "low"), (3, "high")])
    assert labels(h.generalize(obj([1, np.int64(3)]), 1)) == ["low", "high"]
    assert labels(h.generalize(obj([np.int64(2)]), 1)) == ["low"]
    assert check_hierarchy(h, obj(["1"])).rows_not_covered == 1


@pytest.mark.parametrize("cells", [[1, True], [True, 1], [1, 1.0], [1.0, 1]])
def test_true_and_float_do_not_match_int_key(cells: list[object]) -> None:
    h = category_hierarchy([(1, "low")])
    assert check_hierarchy(h, obj(cells)).rows_not_covered == 1
    with pytest.raises(ValueError, match="1 rows"):
        h.generalize(obj(cells), 1)


def test_not_a_tree_names_the_level_only() -> None:
    rows = [("a", "g1", "top1"), ("b", "g1", "top2")]
    with pytest.raises(ValueError, match="level-1 label maps to more than one level-2") as exc:
        category_hierarchy(rows)
    assert "g1" not in str(exc.value) and "top" not in str(exc.value)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [("a",)],
        [("a", "x"), ("b", "x", "y")],
        [("a", "x"), ("a", "y")],
        [(True, "x")],
        [(1.0, "x")],
        [(np.int64(1), "x")],
        [("a", 1)],
        "ab",
        ["ab"],
    ],
)
def test_refusals(rows: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        category_hierarchy(rows)  # type: ignore[arg-type]

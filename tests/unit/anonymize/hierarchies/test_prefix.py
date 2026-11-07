"""Prefix (redaction) hierarchy: literal levels, fixed length, Arrow inputs, refusals."""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.anonymize.hierarchies import PrefixHierarchy, check_hierarchy, prefix_hierarchy

from ._cols import labels, obj


def test_default_levels() -> None:
    h = prefix_hierarchy(5)
    assert (h.height, h.length, h.keep, h.kind) == (5, 5, (4, 3, 2, 1), "prefix")
    expected = ["02134", "0213*", "021**", "02***", "0****", "*"]
    assert [labels(h.generalize(obj(["02134"]), level))[0] for level in range(6)] == expected


def test_chosen_levels_and_nulls() -> None:
    h = prefix_hierarchy(5, keep=(3, 1))
    assert h.height == 3
    column = obj(["02134", None, "90210"])
    assert labels(h.generalize(column, 1)) == ["021**", None, "902**"]
    assert labels(h.generalize(column, 2)) == ["0****", None, "9****"]
    assert labels(h.generalize(column, 3)) == ["*", "*", "*"]


def test_other_lengths_and_numbers_not_covered() -> None:
    h = prefix_hierarchy(5)
    column = obj(["2134", "021345", 2134, "02134"])
    report = check_hierarchy(h, column)
    assert (report.rows_not_covered, report.covers_column) == (3, False)
    with pytest.raises(ValueError, match="3 rows") as exc:
        h.generalize(column, 1)
    assert "2134" not in str(exc.value)


@pytest.mark.parametrize(
    "array",
    [
        pa.array(["02134", "90210", None], pa.large_string()),
        pa.array(["02134", "90210", None]).dictionary_encode(),
        pa.chunked_array([pa.array(["02134"]), pa.array(["90210", None])]),
    ],
)
def test_arrow_inputs(array: pa.Array) -> None:
    out = prefix_hierarchy(5).generalize(array, 2)
    assert isinstance(out, pa.Array) and out.type == pa.string()
    assert out.to_pylist() == ["021**", "902**", None]


@pytest.mark.parametrize(
    ("length", "keep"),
    [(1, None), (True, None), (5.0, None), (np.int64(5), None), (5, ()), (5, (5,)), (5, (0,)),
     (5, (2, 3)), (5, (3, 3)), (5, (True,)), (5, "3"), (5, (np.int64(3),))],
)  # fmt: skip
def test_refusals(length: object, keep: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        prefix_hierarchy(length, keep=keep)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("length", "keep"),
    [(1, (1,)), (True, (1,)), (5, ()), (5, (5,)), (5, (2, 3)), (5, "3"), (5, None)],
)
def test_constructor_refusals(length: object, keep: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        PrefixHierarchy(length=length, keep=keep)  # type: ignore[arg-type]


def test_constructor_matches_builder() -> None:
    assert PrefixHierarchy(length=5, keep=[4, 3, 2, 1]) == prefix_hierarchy(5)  # type: ignore[arg-type]

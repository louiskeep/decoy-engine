"""The lattice ceiling is checked after argument types and before any cell is read."""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.anonymize.hierarchies import Lattice, category_hierarchy, interval_hierarchy
from decoy_engine.anonymize.search import MAX_LATTICE_NODES, search_lattice
from decoy_engine.quality.anonymity import KAnonymity


class _Untouchable(pd.DataFrame):
    """A table that fails the test if any column or cell is read."""

    @property
    def _constructor(self) -> type[pd.DataFrame]:
        return _Untouchable

    def __getitem__(self, key: object) -> object:
        raise AssertionError("the table was read")

    @property
    def loc(self) -> object:
        raise AssertionError("the table was read")

    @property
    def iloc(self) -> object:
        raise AssertionError("the table was read")


def _wide(heights: tuple[int, ...]) -> Lattice:
    """One interval hierarchy per height; height h needs h - 1 widths."""
    out = {}
    for i, h in enumerate(heights):
        widths = tuple(2**j for j in range(1, h))
        out[f"c{i}"] = interval_hierarchy(widths)
    return Lattice(out)


@pytest.mark.parametrize("heights", [(3, 3, 4, 4, 4, 4), (9, 9, 9, 9)])
def test_oversized_lattice_refused_before_reading(heights: tuple[int, ...]) -> None:
    lattice = _wide(heights)
    assert lattice.size > MAX_LATTICE_NODES
    table = _Untouchable({f"c{i}": [1] for i in range(len(heights))})
    with pytest.raises(ValueError, match="ceiling") as info:
        search_lattice(table, lattice, (KAnonymity(2),))
    assert str(lattice.size) in str(info.value)


def test_just_over_the_ceiling() -> None:
    # 10 x 501 = 5,010 nodes.
    lattice = Lattice(
        {
            "a": interval_hierarchy(tuple(2**j for j in range(1, 9))),
            "b": category_hierarchy(
                [(i, *(f"g{i // 2**j}" for j in range(1, 500))) for i in range(4)]
            ),
        }
    )
    assert lattice.size == MAX_LATTICE_NODES + 10
    with pytest.raises(ValueError, match="ceiling"):
        search_lattice(_Untouchable({"a": [1], "b": [1]}), lattice, (KAnonymity(2),))


def test_argument_types_are_checked_before_the_ceiling() -> None:
    lattice = _wide((3, 3, 4, 4, 4, 4))
    table = _Untouchable({f"c{i}": [1] for i in range(6)})
    with pytest.raises(TypeError, match="models"):
        search_lattice(table, lattice, "k")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="suppression_limit"):
        search_lattice(table, lattice, (KAnonymity(2),), suppression_limit=True)


def test_a_lattice_at_the_ceiling_is_searched() -> None:
    # 10 x 500 = 5,000 nodes, a one-row table: found at the bottom.
    lattice = Lattice(
        {
            "a": interval_hierarchy(tuple(2**j for j in range(1, 9))),
            "b": category_hierarchy([(0, *(f"g{j}" for j in range(1, 499)))]),
        }
    )
    assert lattice.size == MAX_LATTICE_NODES
    result = search_lattice(pd.DataFrame({"a": [1], "b": [0]}), lattice, (KAnonymity(1),))
    assert result.node == (0, 0)


def test_mixed_heights_at_the_ceiling_are_searched() -> None:
    # 5 * 5 * 10 * 20 = 5,000 nodes.
    lattice = _wide((4, 4, 9, 19))
    assert lattice.size == MAX_LATTICE_NODES
    table = pd.DataFrame({f"c{i}": [1, 1] for i in range(4)})
    result = search_lattice(table, lattice, (KAnonymity(2),))
    assert result.node == (0,) * 4

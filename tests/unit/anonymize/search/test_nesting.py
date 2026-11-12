"""Optimality needs nested levels; a hierarchy whose level splits a class is refused."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd
import pytest

from decoy_engine.anonymize.hierarchies import Hierarchy, Lattice
from decoy_engine.anonymize.search import search_lattice
from decoy_engine.quality.anonymity import KAnonymity

from ._fixtures import six_rows, small_lattice


class _Crossing(Hierarchy):
    """Level 1 by parity, level 2 by size: level 2 splits both parity classes."""

    __slots__ = ()
    kind = "test-crossing"

    @property
    def height(self) -> int:
        return 3

    def _covers(self, value: Any) -> bool:
        return isinstance(value, int)

    def _labels(self, values: Sequence[Any], level: int) -> list[str]:
        if level == 1:
            return ["even" if v % 2 == 0 else "odd" for v in values]
        return ["low" if v < 5 else "high" for v in values]


def test_non_nested_hierarchy_is_refused() -> None:
    table = pd.DataFrame({"n": [1, 2, 7, 8], "secret": ["SECRET-1", "x", "y", "z"]})
    lattice = Lattice({"n": _Crossing()})
    with pytest.raises(ValueError, match="splits a class") as info:
        search_lattice(table, lattice, (KAnonymity(2),))
    message = str(info.value)
    assert "'n'" in message and "level 2" in message
    for cell in ("1", "2", "7", "8", "even", "odd", "low", "high"):
        assert f"'{cell}'" not in message
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_builders_are_nested() -> None:
    result = search_lattice(six_rows(), small_lattice(), (KAnonymity(2),))
    assert result.found

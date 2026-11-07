"""The generalization lattice: one level per quasi-identifier.

Pattern: full-domain generalization lattice (Samarati 2001; LeFevre, DeWitt and Ramakrishnan 2005 Incognito; Kohlmayer et al. 2012 Flash).
  A node is a level vector, one level per column; nodes are ordered
  componentwise, a successor raises one column by one level and a
  predecessor lowers one. Applying a node recodes every row of each column to
  that column's level (global recoding). See: P. Samarati, "Protecting
  respondents' identities in microdata release", IEEE TKDE 13(6), 2001;
  F. Kohlmayer et al., "Flash: efficient, stable and optimal k-anonymity",
  PASSAT 2012.

Column order is the insertion order of the mapping given to ``Lattice`` and
fixes the meaning of each position in a node. ``apply`` is stateless and
uncached; a search should cache generalized columns per (column, level).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import cast

import pandas as pd
import pyarrow as pa

from decoy_engine.anonymize.hierarchies._base import Hierarchy

Node = tuple[int, ...]


class Lattice:
    """The lattice of level vectors over named hierarchies."""

    __slots__ = ("_columns", "_heights", "_hierarchies")

    def __init__(self, hierarchies: Mapping[str, Hierarchy]) -> None:
        if not isinstance(hierarchies, Mapping):
            raise TypeError("hierarchies must be a mapping of column name to Hierarchy")
        if not hierarchies:
            raise ValueError("hierarchies must name at least one column")
        for name, hierarchy in hierarchies.items():
            if type(name) is not str:
                raise TypeError("column names must be str")
            if not isinstance(hierarchy, Hierarchy):
                raise TypeError(f"the hierarchy for column {name!r} is not a Hierarchy")
        self._hierarchies = MappingProxyType(dict(hierarchies))
        self._columns = tuple(self._hierarchies)
        self._heights = tuple(h.height for h in self._hierarchies.values())

    @property
    def hierarchies(self) -> Mapping[str, Hierarchy]:
        return self._hierarchies

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def heights(self) -> tuple[int, ...]:
        return self._heights

    @property
    def size(self) -> int:
        return math.prod(h + 1 for h in self._heights)

    @property
    def bottom(self) -> Node:
        return tuple(0 for _ in self._heights)

    @property
    def top(self) -> Node:
        return self._heights

    def contains(self, node: object) -> bool:
        return (
            isinstance(node, tuple)
            and len(node) == len(self._heights)
            and all(
                type(v) is int and 0 <= v <= h for v, h in zip(node, self._heights, strict=False)
            )
        )

    def _check(self, node: object) -> Node:
        if not isinstance(node, tuple) or any(type(v) is not int for v in node):
            raise TypeError("node must be a tuple of ints")
        if not self.contains(node):
            raise ValueError("node is not in the lattice (wrong length or a level out of range)")
        return node

    def successors(self, node: Node) -> tuple[Node, ...]:
        node = self._check(node)
        return tuple(
            (*node[:i], v + 1, *node[i + 1 :])
            for i, (v, h) in enumerate(zip(node, self._heights, strict=False))
            if v < h
        )

    def predecessors(self, node: Node) -> tuple[Node, ...]:
        node = self._check(node)
        return tuple((*node[:i], v - 1, *node[i + 1 :]) for i, v in enumerate(node) if v > 0)

    def nodes(self) -> Iterator[Node]:
        """Every node, lazily, in lexicographic order."""
        return itertools.product(*(range(h + 1) for h in self._heights))

    def apply(self, table: pd.DataFrame | pa.Table, node: Node) -> pd.DataFrame | pa.Table:
        """A new table with each lattice column generalized to its level in ``node``.

        A column at level 0 is checked for coverage and left exactly as it was
        (type, chunking, field metadata); other columns are replaced by their
        labels, keeping the Arrow field's nullability and metadata.
        """
        node = self._check(node)
        if isinstance(table, pd.DataFrame):
            names = list(table.columns)
        elif isinstance(table, pa.Table):
            names = list(table.column_names)
        else:
            raise TypeError("table must be a pandas.DataFrame or a pyarrow.Table")
        for name in self._columns:
            if names.count(name) != 1:
                raise ValueError(f"column {name!r} must appear exactly once in the table")
        out = table.copy(deep=False) if isinstance(table, pd.DataFrame) else table
        for name, level in zip(self._columns, node, strict=False):
            hierarchy = self._hierarchies[name]
            error: tuple[type[Exception], str] | None = None
            try:
                if isinstance(out, pd.DataFrame):
                    generalized = hierarchy.generalize(table[name], level)
                    if level:
                        out[name] = generalized
                else:
                    position = names.index(name)
                    array = cast(pa.Array, hierarchy.generalize(table.column(position), level))
                    if level:
                        # Keep the field's nullability and metadata; only the type changes.
                        field = table.schema.field(position).with_type(array.type)
                        out = out.set_column(position, field, array)
            except (TypeError, ValueError) as exc:
                # Hierarchy errors are value-free; the message is kept, the chain is not.
                error = (TypeError if isinstance(exc, TypeError) else ValueError, str(exc))
            if error is not None:
                raise error[0](f"column {name!r}: {error[1]}")
        return out

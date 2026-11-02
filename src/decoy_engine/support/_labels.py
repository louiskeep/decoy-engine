"""`LabelAllocator`: ordinal labels for table, column and node names, by first-seen position.

A label is a position, never a function of the name's bytes: no key, no hash, nothing to
reverse. One allocator per bundle build keeps `t3` meaning the same table in every section; it
is dropped with the build, so no name-to-label map outlives it.

Pattern: `sos clean` sequential obfuscation tokens (`host0`, `host1`; Red Hat, "Cleaning an sos
report"), without the persisted private map.
"""

from __future__ import annotations

from collections.abc import Hashable

from decoy_engine.support._shape import _LABEL_TOKEN, Label

_MAX_ORDINAL = 9999


def _ordinal(seen: dict[Hashable, int], key: Hashable) -> int:
    if key not in seen:
        if len(seen) >= _MAX_ORDINAL:
            raise ValueError("more than 9999 labels of one kind")
        seen[key] = len(seen) + 1
    return seen[key]


class LabelAllocator:
    """Allocates `t<k>`, `t<k>.c<j>` and `n<k>` in first-seen order, starting at 1."""

    def __init__(self) -> None:
        self._tables: dict[Hashable, int] = {}
        self._columns: dict[Hashable, dict[Hashable, int]] = {}
        self._nodes: dict[Hashable, int] = {}

    def table(self, name: Hashable) -> Label:
        return Label(f"t{_ordinal(self._tables, name)}", _LABEL_TOKEN)

    def column(self, table_name: Hashable, column_name: Hashable) -> Label:
        table = self.table(table_name)
        j = _ordinal(self._columns.setdefault(table_name, {}), column_name)
        return Label(f"{table}.c{j}", _LABEL_TOKEN)

    def node(self, node_id: Hashable) -> Label:
        return Label(f"n{_ordinal(self._nodes, node_id)}", _LABEL_TOKEN)

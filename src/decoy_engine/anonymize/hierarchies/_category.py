"""Categorical hierarchy from a user-supplied table (a value generalization tree).

Pattern: value generalization hierarchy (Sweeney 2002; Samarati 2001) in ARX's table form.
  One row per value: the value, then its label at each coarser level, as in
  an ARX hierarchy file. The table must describe a tree: a value appears in
  one row, and a label at level ``i`` maps to one label at level ``i + 1``.
  This is also sdcMicro's ``groupAndRename`` applied level by level. See:
  L. Sweeney, "Achieving k-anonymity privacy protection using generalization
  and suppression", IJUFKS 10(5), 2002; P. Samarati, "Protecting respondents'
  identities in microdata release", IEEE TKDE 13(6), 2001.

Keys are ``str`` or ``int``. A data value matches a key only when it is equal
and of the same type family (``decoy_engine.anonymize.type_family``), so
``True`` and ``1.0`` do not match key ``1`` while ``numpy.int64(1)`` does.
Labels are ``str`` and may not be ``"*"``: the top level (``"*"``) is added,
not written in the rows, and a ``"*"`` lower down would look suppressed
without being the single top class.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar

from decoy_engine.anonymize._values import type_family
from decoy_engine.anonymize.hierarchies._base import SUPPRESSED, Hierarchy

Key = str | int


@dataclass(frozen=True, slots=True)
class CategoryHierarchy(Hierarchy):
    """Rows of ``(value, level-1 label, level-2 label, ...)``."""

    rows: tuple[tuple[Key | str, ...], ...]
    kind: ClassVar[str] = "category"
    _lookup: Mapping[tuple[str, Any], tuple[str, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", _checked_rows(self.rows))
        lookup = {(type_family(row[0]), row[0]): tuple(row[1:]) for row in self.rows}
        object.__setattr__(self, "_lookup", MappingProxyType(lookup))

    @property
    def height(self) -> int:
        return len(self.rows[0])

    def _covers(self, value: Any) -> bool:
        return (type_family(value), value) in self._lookup

    def _labels(self, values: Sequence[Any], level: int) -> list[str]:
        return [self._lookup[(type_family(value), value)][level - 1] for value in values]


def category_hierarchy(rows: Sequence[Sequence[Key | str]]) -> CategoryHierarchy:
    """A tree given as rows ``(value, label at level 1, ..., label at level h-1)``."""
    return CategoryHierarchy(rows=rows)  # type: ignore[arg-type]


def _checked_rows(rows: object) -> tuple[tuple[Any, ...], ...]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise TypeError("rows must be a sequence of rows")
    table: list[tuple[Any, ...]] = []
    for row in rows:
        if isinstance(row, (str, bytes)) or not isinstance(row, Sequence):
            raise TypeError("each row must be a sequence (value, labels...)")
        table.append(tuple(row))
    if not table:
        raise ValueError("rows must hold at least one row")
    width = len(table[0])
    if width < 2:
        raise ValueError("each row needs a value and at least one label")
    seen: set[tuple[str, Any]] = set()
    for number, row in enumerate(table):
        if len(row) != width:
            raise ValueError(f"row {number} has a different number of levels from row 0")
        key = row[0]
        if type(key) not in (str, int):
            raise TypeError(f"row {number}: the value must be a str or an int (not bool or float)")
        for label in row[1:]:
            if type(label) is not str:
                raise TypeError(f"row {number}: labels must be str")
            if label == SUPPRESSED:
                raise ValueError(f'row {number}: "*" is reserved for the top level')
        identity = (type_family(key), key)
        if identity in seen:
            raise ValueError(f"row {number}: the value appears in an earlier row")
        seen.add(identity)
    for level in range(1, width - 1):
        parent: dict[str, str] = {}
        for row in table:
            if parent.setdefault(row[level], row[level + 1]) != row[level + 1]:
                raise ValueError(
                    f"not a tree: a level-{level} label maps to more than one "
                    f"level-{level + 1} label"
                )
    return tuple(table)

"""Check that a hierarchy is sound on a column: coverage and monotonicity.

Monotone means a coarser level never splits a class of a finer level: two
values with the same label at level ``i`` have the same label at ``i + 1``
(the generalization function of a domain generalization hierarchy; Sweeney
2002, Samarati 2001). The builders are monotone by construction; this check
measures it on the data, as ARX checks a hierarchy against the values it
must cover.

The check never mutates the column and its report is frozen and value-free.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from decoy_engine.anonymize.hierarchies._base import (
    _UNHASHABLE,
    Column,
    Hierarchy,
    distinct_values,
)


@dataclass(frozen=True, slots=True)
class HierarchyCheck:
    """Counts over one column; null rows form one class below the top."""

    height: int
    rows: int
    rows_null: int
    rows_not_covered: int
    covers_column: bool
    classes_per_level: tuple[int, ...]
    monotone: bool
    first_split_level: int | None


def check_hierarchy(hierarchy: Hierarchy, column: Column) -> HierarchyCheck:
    """Coverage, classes per level and monotonicity of ``hierarchy`` on ``column``."""
    if not isinstance(hierarchy, Hierarchy):
        raise TypeError("hierarchy must be a Hierarchy")
    distinct = distinct_values(column)
    codes = distinct.codes
    covered = [
        code
        for code, value in enumerate(distinct.uniques)
        if value is not _UNHASHABLE and hierarchy._covers(value)
    ]
    counts = np.bincount(codes[codes >= 0], minlength=len(distinct.uniques))
    rows_null = int((codes < 0).sum())
    rows_not_covered = int(distinct.rows - rows_null - sum(int(counts[c]) for c in covered))
    present = [c for c in covered if counts[c] > 0]
    values = [distinct.uniques[c] for c in present]

    # Per level, the class of each present covered value; level 0 is the value itself.
    height = hierarchy.height
    per_level: list[list[object]] = [list(range(len(values)))]
    for level in range(1, height):
        per_level.append(list(hierarchy._labels(values, level)) if values else [])
    per_level.append([0] * len(values))

    has_null = rows_null > 0
    classes = []
    for level, labels in enumerate(per_level):
        count = len(set(labels))
        if has_null and level < height:
            count += 1
        elif has_null and level == height and not labels:
            count = 1
        classes.append(count)

    first_split: int | None = None
    for level in range(height):
        parent: dict[object, object] = {}
        if any(
            parent.setdefault(fine, coarse) != coarse
            for fine, coarse in zip(per_level[level], per_level[level + 1], strict=False)
        ):
            first_split = level
            break

    return HierarchyCheck(
        height=height,
        rows=distinct.rows,
        rows_null=rows_null,
        rows_not_covered=rows_not_covered,
        covers_column=rows_not_covered == 0,
        classes_per_level=tuple(classes),
        monotone=first_split is None,
        first_split_level=first_split,
    )

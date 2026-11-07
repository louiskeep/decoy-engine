"""Date hierarchy: date or timestamp to month, quarter and year.

Pattern: ARX date-based hierarchy (Prasser and Kohlmayer, ARX Data Anonymization Tool, Apache-2.0).
  A fixed granularity chain where each step is a calendar period that nests
  in the next (month in quarter in year). ISO weeks are not offered: a week
  can span two months, so week to month would split a class. See:
  https://github.com/arx-deidentifier/arx (HierarchyBuilderDate).

Labels use the value's own wall-time year and month: ``"2024-03"``,
``"2024Q1"``, ``"2024"``, with the year zero-padded to four digits (the
pandas ``Period`` spelling for years 1000-9999). A tz-aware value is labelled
in its own zone; an object column must hold one zone (checked in the base).
Covered values: ``datetime.date``, ``datetime.datetime``, ``pandas.Timestamp``
and ``numpy.datetime64`` within the pandas timestamp range. Strings are not
parsed and are not covered.
"""

from __future__ import annotations

import datetime
import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from decoy_engine.anonymize.hierarchies._base import Hierarchy

DATE_LEVELS = ("month", "quarter", "year")


def _year_month(value: Any) -> tuple[int, int] | None:
    if isinstance(value, np.datetime64):
        failed = False
        try:
            value = pd.Timestamp(value)
        except (ValueError, OverflowError):
            failed = True
        if failed or value is pd.NaT:
            return None
    if isinstance(value, datetime.date):
        return value.year, value.month
    return None


@dataclass(frozen=True, slots=True)
class DateHierarchy(Hierarchy):
    """Calendar levels, a subsequence of ``("month", "quarter", "year")``."""

    levels: tuple[str, ...]
    kind: ClassVar[str] = "date"

    @property
    def height(self) -> int:
        return len(self.levels) + 1

    def _covers(self, value: Any) -> bool:
        return _year_month(value) is not None

    def _labels(self, values: Sequence[Any], level: int) -> list[str]:
        unit = self.levels[level - 1]
        out = []
        for value in values:
            year, month = _year_month(value) or (0, 0)
            if unit == "month":
                out.append(f"{year:04d}-{month:02d}")
            elif unit == "quarter":
                out.append(f"{year:04d}Q{(month - 1) // 3 + 1}")
            else:
                out.append(f"{year:04d}")
        return out


def date_hierarchy(levels: Sequence[str] = DATE_LEVELS) -> DateHierarchy:
    """Calendar levels, in the order month, quarter, year (any non-empty subsequence)."""
    if isinstance(levels, (str, bytes)) or not isinstance(levels, Sequence):
        raise TypeError("levels must be a sequence of level names")
    chosen = tuple(levels)
    if not chosen:
        raise ValueError("levels must name at least one level")
    for name in chosen:
        if type(name) is not str or name not in DATE_LEVELS:
            raise ValueError("levels must be drawn from 'month', 'quarter', 'year'")
    positions = [DATE_LEVELS.index(name) for name in chosen]
    if any(b <= a for a, b in itertools.pairwise(positions)):
        raise ValueError("levels must be in the order month, quarter, year, without repeats")
    return DateHierarchy(levels=chosen)

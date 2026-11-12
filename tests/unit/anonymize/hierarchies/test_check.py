"""check_hierarchy and the shared value-identity rule."""

from __future__ import annotations

import datetime
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.anonymize import type_family
from decoy_engine.anonymize.hierarchies import Hierarchy, check_hierarchy, interval_hierarchy

from ._cols import obj

# The families quality.anonymity also uses (it imports this same function).
FAMILIES = [
    (datetime.timedelta(days=1), "timedelta"),
    (np.timedelta64(1, "D"), "timedelta"),
    (datetime.datetime(2024, 1, 1), "datetime"),
    (pd.Timestamp("2024-01-01"), "datetime"),
    (np.datetime64("2024-01-01"), "datetime"),
    (True, "bool"),
    (np.bool_(True), "bool"),
    (1, "int"),
    (np.int64(1), "int"),
    (np.uint8(1), "int"),
    (1.0, "float"),
    (np.float32(1.0), "float"),
    (1j, "complex"),
    (np.complex128(1j), "complex"),
    (Decimal("1"), "decimal"),
    ("a", "str"),
    (np.str_("a"), "str"),
    (b"a", "bytes"),
    (np.bytes_(b"a"), "bytes"),
    (datetime.date(2024, 1, 1), "date"),
    (datetime.time(12, 0), "time"),
    ([1], "builtins.list"),
]


@pytest.mark.parametrize(("cell", "family"), FAMILIES)
def test_type_family_table(cell: object, family: str) -> None:
    assert type_family(cell) == family


def test_counts_and_classes() -> None:
    h = interval_hierarchy((5, 10, 20))
    column = obj([0, 4, 5, 17, 18, 29, 30, 64, 65, 99, None, math.nan, "x", [1]])
    report = check_hierarchy(h, column)
    assert report.rows == 14 and report.rows_null == 2 and report.rows_not_covered == 2
    assert not report.covers_column and report.monotone and report.first_split_level is None
    # 10 distinct values + the null class, then 9, 7 and 5 labels + null, then one "*".
    assert report.classes_per_level == (11, 9, 7, 5, 1)


def test_empty_column() -> None:
    report = check_hierarchy(interval_hierarchy((5,)), pd.Series([], dtype="int64"))
    assert (report.rows, report.classes_per_level, report.monotone) == (0, (0, 0, 0), True)


@dataclass(frozen=True, slots=True)
class _Splitting(Hierarchy):
    """Test-only kind: one level-1 class, split again at level 2."""

    kind: ClassVar[str] = "test-splitting"

    @property
    def height(self) -> int:
        return 3

    def _covers(self, value: Any) -> bool:
        return isinstance(value, int)

    def _labels(self, values: Sequence[Any], level: int) -> list[str]:
        return ["all" if level == 1 else str(v % 2) for v in values]


def test_detects_a_split() -> None:
    report = check_hierarchy(_Splitting(), pd.Series([1, 2, 3]))
    assert (report.monotone, report.first_split_level) == (False, 1)


def test_never_mutates() -> None:
    series = pd.Series([3.0, None, 7.5], index=[5, 6, 7], name="age")
    before = series.copy(deep=True)
    array = pa.array([1, None, 2])
    check_hierarchy(interval_hierarchy((5,)), series)
    check_hierarchy(interval_hierarchy((5,)), array)
    pd.testing.assert_series_equal(series, before)
    assert array.equals(pa.array([1, None, 2]))


def test_refuses_non_hierarchy() -> None:
    with pytest.raises(TypeError):
        check_hierarchy(object(), pd.Series([1]))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        check_hierarchy(interval_hierarchy((5,)), [1, 2])  # type: ignore[arg-type, unused-ignore]

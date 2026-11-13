"""AT-E2-7: the one missing-cell predicate shared by kernels, derived and formula."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.internal.pandas_missing import is_null_cell

_TABLE: list[tuple[Any, bool]] = [
    (None, True),
    (float("nan"), True),
    (np.float32("nan"), True),
    (np.float64("nan"), True),
    (Decimal("NaN"), True),
    (pd.NaT, True),
    (np.datetime64("NaT"), True),
    (pd.NA, True),
    ("", False),
    ("NaT", False),
    (0, False),
    (False, False),
    (pd.Timestamp("2020-01-01"), False),
]


@pytest.mark.parametrize(("value", "expected"), _TABLE, ids=[repr(v) for v, _ in _TABLE])
def test_is_null_cell_truth_table(value: Any, expected: bool) -> None:
    assert is_null_cell(value) is expected


def test_kernel_alias_is_the_same_predicate() -> None:
    from decoy_engine.kernel._scalar import _is_missing

    assert _is_missing is is_null_cell


def test_derived_alias_is_the_same_predicate() -> None:
    from decoy_engine.transforms.derived import _is_null

    assert _is_null is is_null_cell

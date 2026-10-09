"""Step 0 (C6a plan §3b): `_is_missing` must treat `pd.NA` as missing on the raw-list path.

The shipped fpe strategy decides missingness with `source.isna()`, which folds `pd.NA`
to missing. The native reference oracle decides it with `kernel/_scalar._is_missing`,
which on the raw-list path used to return False for `pd.NA` (`bool(pd.NA != pd.NA)`
raises, caught and swallowed to False), so `str(pd.NA) == "<NA>"` reached the cipher and
diverged from the strategy. These tests pin the aligned behavior at the shared source so
every reference kernel agrees; the Arrow fast path already folds NA to null upstream.
"""

from __future__ import annotations

import math

import pandas as pd

from decoy_engine.kernel._scalar import _is_missing


def test_pd_na_is_missing() -> None:
    # The regression Step 0 closes: pd.NA must be missing, matching source.isna().
    assert _is_missing(pd.NA) is True


def test_none_and_nan_still_missing() -> None:
    assert _is_missing(None) is True
    assert _is_missing(float("nan")) is True
    assert _is_missing(math.nan) is True


def test_present_values_not_missing() -> None:
    for value in ["", "abc", "<NA>", 0, 5, -3, False, True, 1.5]:
        assert _is_missing(value) is False, value

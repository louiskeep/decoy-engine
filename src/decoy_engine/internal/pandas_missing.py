"""The one missing-cell predicate shared by the masking kernels, derived and formula."""

from __future__ import annotations

from typing import Any


def is_null_cell(value: Any) -> bool:
    """True for a cell that every transform must treat as missing (null).

    None and IEEE NaN both count, matching pandas ``isna()`` and the
    ``pa.array(..., from_pandas=True)`` conversion the full-frame pandas path
    runs BEFORE a value ever reaches these kernels (that conversion folds NaN
    to null). The out-of-core route feeds raw Arrow values straight in, so a
    float column carrying an actual NaN would otherwise be hashed / redacted /
    stringified ("nan") here where the oracle emitted null. Only NaN-like
    values are unequal to themselves, so ``value != value`` detects float,
    numpy-float, and Decimal('NaN') alike without special-casing each type. NaT
    compares unequal to itself too; pd.NA is caught by the pandas fallback below.
    A non-comparable object simply is not missing.
    """
    if value is None:
        return True
    try:
        return bool(value != value)
    except Exception:
        # `value != value` did not yield a plain bool. The reachable case is
        # pandas `pd.NA`, whose `!=` returns `pd.NA` and whose `bool()` raises:
        # the shipped fpe strategy treats it as missing (`source.isna()`), and on
        # the raw-list path it would otherwise reach the cipher as `str(pd.NA) ==
        # "<NA>"` and diverge (C6a Step 0, plan §3b). Defer to pandas.isna, which
        # is the repo's own correct missing-value predicate, imported lazily so
        # the Arrow hot path (None / float NaN, both handled above) never pays it.
        try:
            import pandas as pd

            return bool(pd.isna(value))
        except Exception:
            return False

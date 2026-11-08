"""Direct double-loop DCR over canonical frames: the oracle for the vectorized, chunked version."""

from __future__ import annotations

import math

import pandas as pd


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def brute_nearest(
    query: pd.DataFrame,
    reference: pd.DataFrame,
    columns: tuple[str, ...],
    numeric: tuple[bool, ...],
    ranges: tuple[float, ...],
    *,
    exclude_self: bool = False,
) -> list[float]:
    out = []
    for i in range(len(query)):
        best = math.inf
        for k in range(len(reference)):
            if exclude_self and i == k:
                continue
            total = 0.0
            for j, name in enumerate(columns):
                a, b = query[name].iloc[i], reference[name].iloc[k]
                if _missing(a) and _missing(b):
                    cell = 0.0
                elif _missing(a) or _missing(b):
                    cell = 1.0
                elif numeric[j] and ranges[j] > 0:
                    cell = min(abs(float(a) - float(b)) / ranges[j], 1.0)
                else:
                    cell = 0.0 if a == b else 1.0
                total += cell
            best = min(best, total / len(columns))
        out.append(best)
    return out

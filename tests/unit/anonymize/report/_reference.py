"""Plain-Python reference for the risk section: tuple grouping and ``Fraction`` arithmetic.

Independent of ``quality.anonymity``'s grouping: classes are dict keys built from the
generalized cells (null as one value, value identity by type family).
"""

from __future__ import annotations

from collections import Counter
from fractions import Fraction
from typing import Any

import pandas as pd

from decoy_engine.anonymize import type_family


def _key(cell: Any) -> Any:
    if cell is None or cell is pd.NA or (isinstance(cell, float) and cell != cell):
        return ("null",)
    if cell is pd.NaT:
        return ("null",)
    return (type_family(cell), cell)


def class_sizes(frame: pd.DataFrame, columns: tuple[str, ...]) -> Counter[tuple[Any, ...]]:
    rows = zip(*(frame[c].astype(object).tolist() for c in columns), strict=True)
    return Counter(tuple(_key(cell) for cell in row) for row in rows)


def risk_section(
    released: pd.DataFrame,
    columns: tuple[str, ...],
    tau: float,
    population: pd.DataFrame | None,
) -> dict[str, Any]:
    """Expected ``risk`` section values (floats compared with a 1e-12 relative tolerance)."""
    threshold = Fraction(repr(float(tau)))
    sizes = class_sizes(released, columns)
    n = sum(sizes.values())
    out: dict[str, Any] = {"prosecutor": None, "journalist": None, "marketer": None}
    if n:
        at_risk = sum(f for f in sizes.values() if Fraction(1, f) > threshold)
        out["prosecutor"] = {
            "highest": Fraction(1, min(sizes.values())),
            "average": Fraction(len(sizes), n),
            "records_at_risk": Fraction(at_risk, n),
            "rows_at_risk": at_risk,
        }
    if population is not None and n:
        big = class_sizes(population, columns)
        pairs = [(f, big[key]) for key, f in sizes.items()]
        ratio = sum(Fraction(f, big_f) for f, big_f in pairs) / n
        at_risk = sum(f for f, big_f in pairs if Fraction(1, big_f) > threshold)
        out["journalist"] = {
            "highest": Fraction(1, min(big_f for _, big_f in pairs)),
            "average": max(Fraction(len(pairs), sum(big_f for _, big_f in pairs)), ratio),
            "records_at_risk": Fraction(at_risk, n),
            "rows_at_risk": at_risk,
        }
        out["marketer"] = {"average": ratio}
    return out

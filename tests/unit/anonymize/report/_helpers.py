"""Shared inputs for the report tests: a searched, released table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

from decoy_engine.anonymize.hierarchies import Lattice, interval_hierarchy, prefix_hierarchy
from decoy_engine.anonymize.search import Released, SearchResult, apply_search, search_lattice
from decoy_engine.quality.anonymity import KAnonymity, PrivacyModel


def lattice() -> Lattice:
    """age: value, 5s, 10s, *; zip: value, 2-char, 1-char, * (16 nodes)."""
    return Lattice({"age": interval_hierarchy((5, 10)), "zip": prefix_hierarchy(3, keep=(2, 1))})


def table() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "age": [21, 22, 23, 24, 31, 33, 35, 38, 41, 49, 52, 58],
            "zip": ["121", "121", "122", "122", "130", "131", "130", "131", "210", "211", "210", "212"],
            "dx": ["a", "b", "a", "c", "b", "a", "c", "c", "a", "b", "c", "a"],
        }
    )


def release(
    frame: Any,
    models: Sequence[PrivacyModel] = (KAnonymity(2),),
    limit: float = 0.0,
    *,
    the_lattice: Lattice | None = None,
    **search: Any,
) -> tuple[Lattice, SearchResult, Released]:
    chosen = the_lattice or lattice()
    result = search_lattice(frame, chosen, tuple(models), suppression_limit=limit, **search)
    return chosen, result, apply_search(frame, chosen, result)

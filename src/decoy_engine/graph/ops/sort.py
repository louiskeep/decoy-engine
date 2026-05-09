"""sort — order rows by one or more columns.

Config:
    by: list[str]                          - columns to sort by (required)
    order: 'asc' | 'desc' | list[str]      - per-column or uniform direction
                                             (default 'asc' for all)

Pandas does the heavy lifting; we surface bad column names as OpError so they
land in the node-run record instead of a stack trace.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "sort"
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"

_DIRECTIONS = ("asc", "desc")


def validate_config(config: dict[str, Any]) -> None:
    by = config.get("by")
    if not isinstance(by, list) or not by or not all(isinstance(c, str) and c for c in by):
        raise ValidationError(
            "'by' must be a non-empty list of column names", "config.by"
        )

    order = config.get("order", "asc")
    if isinstance(order, str):
        if order not in _DIRECTIONS:
            raise ValidationError(
                f"'order' must be 'asc' or 'desc' (got {order!r})", "config.order"
            )
    elif isinstance(order, list):
        if len(order) != len(by):
            raise ValidationError(
                f"'order' list length ({len(order)}) must match 'by' length ({len(by)})",
                "config.order",
            )
        if not all(o in _DIRECTIONS for o in order):
            raise ValidationError(
                "every entry in 'order' must be 'asc' or 'desc'", "config.order"
            )
    else:
        raise ValidationError(
            "'order' must be a string or list of strings", "config.order"
        )


def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0]
    by = config["by"]
    missing = [c for c in by if c not in df.columns]
    if missing:
        raise OpError(f"sort columns not in input: {missing}")

    order = config.get("order", "asc")
    if isinstance(order, str):
        ascending = order == "asc"
    else:
        ascending = [o == "asc" for o in order]

    try:
        return df.sort_values(by=by, ascending=ascending, kind="mergesort").reset_index(drop=True)
    except Exception as exc:
        raise OpError(f"sort failed: {exc}") from exc

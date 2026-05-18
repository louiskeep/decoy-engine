"""sort — order rows by one or more columns.

Config:
    by: list[str]                          - columns to sort by (required)
    order: 'asc' | 'desc' | list[str]      - per-column or uniform direction
                                             (default 'asc' for all)

Phase 3 of the polars-duckdb hybrid plan: sort declares NATIVE_ENGINE='polars'.
The runner activates the polars implementation when the graph YAML opts in
via `engine: hybrid`; pandas mode (today's default) routes to the legacy
pandas implementation. Both paths produce identical output for the sort
keys we support — verified by the parity test suite in tests/parity/.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError, is_polars_frame
from decoy_engine.internal.validator import ValidationError

KIND = "sort"
NATIVE_ENGINE = "polars"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"

_DIRECTIONS = ("asc", "desc")


def validate_config(config: dict[str, Any]) -> None:
    from decoy_engine.validation_result import CODES
    by = config.get("by")
    if not isinstance(by, list) or not by or not all(isinstance(c, str) and c for c in by):
        raise ValidationError(
            "'by' must be a non-empty list of column names",
            "config.by",
            code=CODES.SORT_MISSING_BY,
        )

    order = config.get("order", "asc")
    if isinstance(order, str):
        if order not in _DIRECTIONS:
            raise ValidationError(
                f"'order' must be 'asc' or 'desc' (got {order!r})",
                "config.order",
                code=CODES.SORT_BAD_ORDER,
            )
    elif isinstance(order, list):
        if len(order) != len(by):
            raise ValidationError(
                f"'order' list length ({len(order)}) must match 'by' length ({len(by)})",
                "config.order",
                code=CODES.SORT_ORDER_LENGTH_MISMATCH,
            )
        if not all(o in _DIRECTIONS for o in order):
            raise ValidationError(
                "every entry in 'order' must be 'asc' or 'desc'",
                "config.order",
                code=CODES.SORT_BAD_ORDER,
            )
    else:
        raise ValidationError(
            "'order' must be a string or list of strings",
            "config.order",
            code=CODES.SORT_BAD_ORDER,
        )


def apply(inputs, config, ctx):
    df = inputs[0]
    # Type-dispatch keeps polars optional: pandas-only installs never import
    # polars and never reach the polars branch.
    if is_polars_frame(df):
        return _apply_polars(df, config)
    return _apply_pandas(df, config)


def _apply_pandas(df: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
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


def _apply_polars(df, config: dict[str, Any]):
    by = config["by"]
    missing = [c for c in by if c not in df.columns]
    if missing:
        raise OpError(f"sort columns not in input: {missing}")

    order = config.get("order", "asc")
    if isinstance(order, str):
        descending = order == "desc"
    else:
        descending = [o == "desc" for o in order]

    try:
        # maintain_order=True approximates pandas mergesort stability — same
        # ordering for tied keys. Verified in parity tests.
        return df.sort(by=by, descending=descending, maintain_order=True)
    except Exception as exc:
        raise OpError(f"sort failed: {exc}") from exc



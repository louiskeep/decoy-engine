"""dedupe — drop duplicate rows.

Config:
    on: list[str]      - columns to consider (defaults to all)
    keep: 'first' | 'last'  - which duplicate to keep (default: 'first')

Phase 3 port: NATIVE_ENGINE='polars'. Pandas fallback retained for
graph engine mode = pandas (today's default until Phase 8).
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError, is_polars_frame
from decoy_engine.internal.validator import ValidationError

KIND = "dedupe"
NATIVE_ENGINE = "polars"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    on = config.get("on")
    if on is not None:
        if not isinstance(on, list) or not all(isinstance(c, str) for c in on):
            raise ValidationError("'on' must be a list of strings", "config.on")
    keep = config.get("keep", "first")
    if keep not in ("first", "last"):
        raise ValidationError("'keep' must be 'first' or 'last'", "config.keep")


def apply(inputs, config, ctx):
    df = inputs[0]
    if is_polars_frame(df):
        return _apply_polars(df, config)
    return _apply_pandas(df, config)


def _apply_pandas(df: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    on = config.get("on")
    keep = config.get("keep", "first")
    if on:
        missing = [c for c in on if c not in df.columns]
        if missing:
            raise OpError(f"dedupe columns not in input: {missing}")
    return df.drop_duplicates(subset=on, keep=keep)


def _apply_polars(df, config: dict[str, Any]):
    on = config.get("on")
    keep = config.get("keep", "first")
    if on:
        missing = [c for c in on if c not in df.columns]
        if missing:
            raise OpError(f"dedupe columns not in input: {missing}")
    # Polars `.unique(keep='first' | 'last' | 'any' | 'none')` — first/last
    # match pandas semantics. maintain_order keeps the row position stable
    # across runs, matching `drop_duplicates` default.
    return df.unique(subset=on, keep=keep, maintain_order=True)

"""dedupe — drop duplicate rows.

Config:
    on: list[str]      - columns to consider (defaults to all)
    keep: 'first' | 'last'  - which duplicate to keep (default: 'first')
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "dedupe"
NATIVE_ENGINE = "pandas"
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


def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0]
    on = config.get("on")
    keep = config.get("keep", "first")
    if on:
        missing = [c for c in on if c not in df.columns]
        if missing:
            raise OpError(f"dedupe columns not in input: {missing}")
    return df.drop_duplicates(subset=on, keep=keep)

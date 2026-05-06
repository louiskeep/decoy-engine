"""drop_column — drop named columns from the input DataFrame.

Config:
    columns: list[str]
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "drop_column"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    cols = config.get("columns")
    if not isinstance(cols, list) or not cols:
        raise ValidationError(
            "'columns' must be a non-empty list", "config.columns"
        )
    if not all(isinstance(c, str) for c in cols):
        raise ValidationError("'columns' entries must be strings", "config.columns")


def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0]
    columns = config["columns"]
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise OpError(f"columns not in input: {missing}")
    return df.drop(columns=columns)

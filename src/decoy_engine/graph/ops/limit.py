"""limit — keep the first N rows (pairs with sort for top-N queries).

Config:
    n: int   - number of rows to keep (must be >= 0)
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import is_polars_frame
from decoy_engine.internal.validator import ValidationError

KIND = "limit"
NATIVE_ENGINE = "polars"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    n = config.get("n")
    # bool is an int subclass in Python; reject it explicitly so True/False don't pass.
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValidationError("'n' must be a non-negative integer", "config.n")


def apply(inputs, config, ctx):
    df = inputs[0]
    n = config["n"]
    if is_polars_frame(df):
        return df.head(n)
    return df.head(n).reset_index(drop=True)

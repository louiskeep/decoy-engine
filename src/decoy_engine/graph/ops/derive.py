"""derive — add a computed column from a pandas eval expression.

Config:
    column: str       - name of the new column (required)
    expression: str   - pandas-eval expression over existing columns

Note: the per-column `formula` strategy (inside Mask) is row-major and runs
inside the masker. `derive` is a graph node that operates on the whole frame
at once and lives outside Mask/Generate, so a YAML can compute a column
without configuring a Mask node just for that one job.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "derive"
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    column = config.get("column")
    if not isinstance(column, str) or not column.strip():
        raise ValidationError("'column' must be a non-empty string", "config.column")

    expression = config.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ValidationError(
            "'expression' must be a non-empty string", "config.expression"
        )


def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0]
    column = config["column"]
    expression = config["expression"]
    out = df.copy()
    try:
        out[column] = df.eval(expression, engine="python")
    except Exception as exc:
        raise OpError(
            f"derive expression failed for column {column!r} ({expression!r}): {exc}"
        ) from exc
    return out

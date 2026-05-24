"""drop_column — drop named columns from the input DataFrame.

Config:
    columns: list[str]
"""

from typing import Any

from decoy_engine.errors import ValidationError
from decoy_engine.graph.ops._base import OpError, is_polars_frame

KIND = "drop_column"
NATIVE_ENGINE = "polars"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    # Empty / missing `columns` is valid: it means "drop nothing" — a
    # no-op pass-through. Matches the canvas's drag-then-configure flow
    # where a freshly-dropped drop_column node sits unconfigured until
    # the user picks columns. Rejecting it before the user gets a
    # chance to configure blocks unrelated samples / runs across the
    # rest of the graph.
    cols = config.get("columns")
    if cols is None:
        return
    if not isinstance(cols, list):
        raise ValidationError("'columns' must be a list", "config.columns")
    if not all(isinstance(c, str) for c in cols):
        raise ValidationError("'columns' entries must be strings", "config.columns")


def apply(inputs, config, ctx):
    df = inputs[0]
    columns = config.get("columns") or []
    if not columns:
        return df  # no-op
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise OpError(f"columns not in input: {missing}")
    if is_polars_frame(df):
        return df.drop(columns)
    return df.drop(columns=columns)

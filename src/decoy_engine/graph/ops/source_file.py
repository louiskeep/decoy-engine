"""source.file — read a CSV/parquet file into a DataFrame.

Config:
    path: str            - filesystem path
    format: 'csv' | 'parquet'  (optional; inferred from extension)
"""

from pathlib import Path
from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "source.file"
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    if "path" not in config:
        raise ValidationError("missing required field 'path'", "config.path")
    fmt = (config.get("format") or _infer_format(config["path"])).lower()
    if fmt not in {"csv", "parquet"}:
        raise ValidationError(
            f"unsupported format {fmt!r} (csv|parquet)", "config.format"
        )


def apply(inputs, config, ctx) -> pd.DataFrame:
    path = Path(config["path"])
    fmt = (config.get("format") or _infer_format(str(path))).lower()
    row_limit = config.get("__preview_row_limit")
    try:
        if fmt == "csv":
            return pd.read_csv(path, nrows=row_limit)
        if fmt == "parquet":
            df = pd.read_parquet(path)
            return df.head(row_limit) if row_limit else df
    except Exception as exc:
        raise OpError(f"failed to read {path}: {exc}") from exc
    raise OpError(f"unsupported format: {fmt}")


def _infer_format(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"parquet", "pq"}:
        return "parquet"
    return "csv"

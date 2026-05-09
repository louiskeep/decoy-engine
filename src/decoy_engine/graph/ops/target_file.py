"""target.file — write a DataFrame to CSV/parquet.

Config:
    output_filename: str  - filesystem path to write
    format: 'csv' | 'parquet'  (optional; inferred from extension)
"""

from pathlib import Path
from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "target.file"
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "sink"


def validate_config(config: dict[str, Any]) -> None:
    if "output_filename" not in config:
        raise ValidationError(
            "missing required field 'output_filename'", "config.output_filename"
        )
    fmt = (config.get("format") or _infer_format(config["output_filename"])).lower()
    if fmt not in {"csv", "parquet"}:
        raise ValidationError(
            f"unsupported format {fmt!r} (csv|parquet)", "config.format"
        )


def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0]
    if config.get("__preview_row_limit") is not None:
        # Preview mode: don't actually write — return what would be written.
        return df
    path = Path(config["output_filename"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (config.get("format") or _infer_format(str(path))).lower()
    try:
        if fmt == "csv":
            df.to_csv(path, index=False)
        elif fmt == "parquet":
            df.to_parquet(path, index=False)
    except Exception as exc:
        raise OpError(f"failed to write {path}: {exc}") from exc
    return df.head(0)  # sinks return empty


def _infer_format(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"parquet", "pq"}:
        return "parquet"
    return "csv"

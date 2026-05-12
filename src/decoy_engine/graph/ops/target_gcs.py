"""target.gcs — write a DataFrame to Google Cloud Storage.

Config:
    bucket: str                - GCS bucket name (required)
    path: str                  - object path within the bucket (required)
    format: str                - 'csv' | 'parquet' (optional; inferred)
    project: str               - GCP project ID (optional)
    service_account_json: str  - JSON SA key content (optional; uses ADC when absent)

Writes to a temp file, then streams bytes via GCSFileSink.write(). Preview
mode skips the upload and returns the DataFrame unchanged.

GCS connector is an optional extra (`pip install decoy-engine[gcs]`).
"""
from typing import Any

from decoy_engine.graph.ops._base import OpError
from decoy_engine.graph.ops._cloud_io import infer_format, validate_format, write_and_upload
from decoy_engine.internal.validator import ValidationError

KIND = "target.gcs"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "sink"


def validate_config(config: dict[str, Any]) -> None:
    if "bucket" not in config:
        raise ValidationError("missing required field 'bucket'", "config.bucket")
    if "path" not in config:
        raise ValidationError("missing required field 'path'", "config.path")
    fmt = (config.get("format") or infer_format(config["path"])).lower()
    validate_format(fmt)


def apply(inputs, config, ctx):
    df = inputs[0]
    if config.get("__preview_row_limit") is not None:
        return df

    try:
        from decoy_engine.connectors.gcs import GCSConfig, GCSFileSink
    except ImportError as exc:
        raise OpError(
            "target.gcs requires google-cloud-storage: "
            "pip install 'decoy-engine[gcs]'"
        ) from exc

    try:
        gcs_config = GCSConfig(
            bucket=config["bucket"],
            prefix=config.get("prefix", ""),
            project=config.get("project"),
            service_account_json=config.get("service_account_json"),
        )
    except Exception as exc:
        raise OpError(f"target.gcs config error: {exc}") from exc

    sink = GCSFileSink(gcs_config)
    try:
        return write_and_upload(df, sink, config["path"], config)
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"target.gcs write failed: {exc}") from exc

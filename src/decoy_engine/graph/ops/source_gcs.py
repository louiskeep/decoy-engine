"""source.gcs — read a file from Google Cloud Storage into a DataFrame.

Config:
    bucket: str                - GCS bucket name (required)
    path: str                  - object path within the bucket (required)
    format: str                - 'csv' | 'parquet' (optional; inferred)
    project: str               - GCP project ID (optional)
    service_account_json: str  - JSON SA key content (optional; uses ADC when absent)

Downloads via GCSFileSource into a temp file, then reads with DuckDB / pandas.

GCS connector is an optional extra (`pip install decoy-engine[gcs]`). If
google-cloud-storage is not installed the op raises OpError rather than
ImportError so the error surfaces clearly at run time.
"""

from typing import Any

from decoy_engine.errors import ValidationError
from decoy_engine.graph.ops._base import OpError
from decoy_engine.graph.ops._cloud_io import download_and_read, infer_format, validate_format

KIND = "source.gcs"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    from decoy_engine.validation_result import CODES

    if "bucket" not in config:
        raise ValidationError(
            "missing required field 'bucket'",
            "config.bucket",
            code=CODES.SOURCE_GCS_MISSING_BUCKET,
        )
    if "path" not in config:
        raise ValidationError(
            "missing required field 'path'",
            "config.path",
            code=CODES.SOURCE_GCS_MISSING_PATH,
        )
    fmt = (config.get("format") or infer_format(config["path"])).lower()
    validate_format(fmt)


def apply(inputs, config, ctx):
    try:
        from decoy_engine.connectors.gcs import GCSConfig, GCSFileSource
    except ImportError as exc:
        raise OpError(
            "source.gcs requires google-cloud-storage: pip install 'decoy-engine[gcs]'"
        ) from exc

    try:
        gcs_config = GCSConfig(
            bucket=config["bucket"],
            prefix=config.get("prefix", ""),
            project=config.get("project"),
            service_account_json=config.get("service_account_json"),
        )
    except Exception as exc:
        raise OpError(f"source.gcs config error: {exc}") from exc

    source = GCSFileSource(gcs_config)
    try:
        return download_and_read(source, config["path"], config)
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"source.gcs read failed: {exc}") from exc

"""source.s3 — read a file from S3 (or S3-compatible storage) into a DataFrame.

Config:
    bucket: str            - S3 bucket name (required)
    path: str              - S3 object key to read (required)
    format: str            - 'csv' | 'parquet' (optional; inferred from path)
    region: str            - AWS region (optional; default 'us-east-1')
    access_key_id: str     - AWS access key (optional; falls back to boto3 chain)
    secret_access_key: str - AWS secret key (optional)
    endpoint_url: str      - S3-compatible endpoint override (optional)
    prefix: str            - key prefix scoping the connector (optional)

Downloads the object via S3FileSource into a temp file, then reads with
DuckDB (or pandas). The read path is identical to source.file so the
hybrid-engine behaviour (DuckDB Arrow table, pandas DataFrame) is preserved.
"""

from typing import Any

from decoy_engine.errors import ValidationError
from decoy_engine.graph.ops._base import OpError
from decoy_engine.graph.ops._cloud_io import download_and_read, infer_format, validate_format

KIND = "source.s3"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    from decoy_engine.validation_result import CODES

    if "bucket" not in config:
        raise ValidationError(
            "missing required field 'bucket'",
            "config.bucket",
            code=CODES.SOURCE_S3_MISSING_BUCKET,
        )
    if "path" not in config:
        raise ValidationError(
            "missing required field 'path'",
            "config.path",
            code=CODES.SOURCE_S3_MISSING_PATH,
        )
    fmt = (config.get("format") or infer_format(config["path"])).lower()
    validate_format(fmt)


def apply(inputs, config, ctx):
    from decoy_engine.connectors.s3 import S3Config, S3FileSource

    try:
        s3_config = S3Config(
            bucket=config["bucket"],
            prefix=config.get("prefix", ""),
            region=config.get("region", "us-east-1"),
            access_key_id=config.get("access_key_id"),
            secret_access_key=config.get("secret_access_key"),
            endpoint_url=config.get("endpoint_url"),
        )
    except Exception as exc:
        raise OpError(f"source.s3 config error: {exc}") from exc

    source = S3FileSource(s3_config)
    try:
        return download_and_read(source, config["path"], config)
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"source.s3 read failed: {exc}") from exc

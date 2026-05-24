"""source.sftp — read a file from an SFTP host into a DataFrame.

Config:
    host: str        - SFTP hostname (required)
    username: str    - SSH username (required)
    path: str        - remote file path to read (required)
    port: int        - SSH port (optional; default 22)
    password: str    - SSH password (provide this or private_key)
    private_key: str - PEM-encoded private key text (provide this or password)
    base_path: str   - base directory on the remote host (optional)
    format: str      - 'csv' | 'parquet' (optional; inferred from path)

Either `password` or `private_key` must be provided. Downloads via
SFTPFileSource into a temp file, then reads with DuckDB / pandas.
close() is called in a finally block to avoid leaking SSH connections.

SFTP connector is an optional extra (`pip install decoy-engine[sftp]`).
"""

from typing import Any

from decoy_engine.graph.ops._base import OpError
from decoy_engine.graph.ops._cloud_io import download_and_read, infer_format, validate_format
from decoy_engine.internal.validator import ValidationError

KIND = "source.sftp"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    from decoy_engine.validation_result import CODES

    if "host" not in config:
        raise ValidationError(
            "missing required field 'host'",
            "config.host",
            code=CODES.SOURCE_SFTP_MISSING_HOST,
        )
    if "username" not in config:
        raise ValidationError(
            "missing required field 'username'",
            "config.username",
            code=CODES.SOURCE_SFTP_MISSING_USERNAME,
        )
    if "path" not in config:
        raise ValidationError(
            "missing required field 'path'",
            "config.path",
            code=CODES.SOURCE_SFTP_MISSING_PATH,
        )
    if not config.get("password") and not config.get("private_key"):
        raise ValidationError(
            "must provide either 'password' or 'private_key'",
            "config",
            code=CODES.SOURCE_SFTP_MISSING_AUTH,
        )
    fmt = (config.get("format") or infer_format(config["path"])).lower()
    validate_format(fmt)


def apply(inputs, config, ctx):
    try:
        from decoy_engine.connectors.sftp import SFTPConfig, SFTPFileSource
    except ImportError as exc:
        raise OpError("source.sftp requires paramiko: pip install 'decoy-engine[sftp]'") from exc

    try:
        sftp_config = SFTPConfig(
            host=config["host"],
            port=config.get("port", 22),
            username=config["username"],
            password=config.get("password"),
            private_key=config.get("private_key"),
            base_path=config.get("base_path", ""),
        )
    except Exception as exc:
        raise OpError(f"source.sftp config error: {exc}") from exc

    source = SFTPFileSource(sftp_config)
    try:
        return download_and_read(source, config["path"], config)
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"source.sftp read failed: {exc}") from exc
    finally:
        source.close()

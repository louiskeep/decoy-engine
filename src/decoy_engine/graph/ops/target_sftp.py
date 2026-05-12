"""target.sftp — write a DataFrame to an SFTP host.

Config:
    host: str        - SFTP hostname (required)
    username: str    - SSH username (required)
    path: str        - remote file path to write (required)
    port: int        - SSH port (optional; default 22)
    password: str    - SSH password (provide this or private_key)
    private_key: str - PEM-encoded private key text (provide this or password)
    base_path: str   - base directory on the remote host (optional)
    format: str      - 'csv' | 'parquet' (optional; inferred from path)

Writes to a temp file, then streams bytes via SFTPFileSink.write(). Preview
mode skips the upload and returns the DataFrame unchanged.
close() is called in a finally block to avoid leaking SSH connections.

SFTP connector is an optional extra (`pip install decoy-engine[sftp]`).
"""
from typing import Any

from decoy_engine.graph.ops._base import OpError
from decoy_engine.graph.ops._cloud_io import infer_format, validate_format, write_and_upload
from decoy_engine.internal.validator import ValidationError

KIND = "target.sftp"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "sink"


def validate_config(config: dict[str, Any]) -> None:
    if "host" not in config:
        raise ValidationError("missing required field 'host'", "config.host")
    if "username" not in config:
        raise ValidationError("missing required field 'username'", "config.username")
    if "path" not in config:
        raise ValidationError("missing required field 'path'", "config.path")
    if not config.get("password") and not config.get("private_key"):
        raise ValidationError(
            "must provide either 'password' or 'private_key'", "config"
        )
    fmt = (config.get("format") or infer_format(config["path"])).lower()
    validate_format(fmt)


def apply(inputs, config, ctx):
    df = inputs[0]
    if config.get("__preview_row_limit") is not None:
        return df

    try:
        from decoy_engine.connectors.sftp import SFTPConfig, SFTPFileSink
    except ImportError as exc:
        raise OpError(
            "target.sftp requires paramiko: "
            "pip install 'decoy-engine[sftp]'"
        ) from exc

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
        raise OpError(f"target.sftp config error: {exc}") from exc

    sink = SFTPFileSink(sftp_config)
    try:
        return write_and_upload(df, sink, config["path"], config)
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"target.sftp write failed: {exc}") from exc
    finally:
        sink.close()

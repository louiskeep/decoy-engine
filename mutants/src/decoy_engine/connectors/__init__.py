"""Connectors package: SDK-based streaming connectors only.

S9 deleted the legacy V1 path-based ``IOHandler`` family (``CSVHandler``,
``FixedWidthHandler``, ``DBHandler``, ``create_io_handler``). The platform
mask + generate paths now run through the V2 ``ExecutionAdapter`` +
``generation.synthesize``, both of which read sources as Arrow + write
outputs via the V2 source/target resolvers (``api.jobs.v2_config``).

What survives is the SDK family: ``S3FileSource`` / ``S3FileSink`` (plus the
optional GCS + SFTP installs). List + open + write semantics; no DataFrame
conversion at the connector layer.
"""

from decoy_engine.connectors.s3 import S3Config, S3FileSink, S3FileSource

# GCS and SFTP connectors are optional installs (`decoy-engine[gcs]` and
# `decoy-engine[sftp]`). Import lazily so a customer who only uses S3
# doesn't see ImportError when google-cloud-storage / paramiko are not
# present. Names still appear in `__all__` so static tools and editors
# can find them when the extras ARE installed.
try:
    from decoy_engine.connectors.gcs import GCSConfig, GCSFileSink, GCSFileSource
except ImportError:
    GCSConfig = GCSFileSink = GCSFileSource = None  # type: ignore[assignment]
try:
    from decoy_engine.connectors.sftp import SFTPConfig, SFTPFileSink, SFTPFileSource
except ImportError:
    SFTPConfig = SFTPFileSink = SFTPFileSource = None  # type: ignore[assignment]

__all__ = [
    "GCSConfig",
    "GCSFileSink",
    "GCSFileSource",
    # SDK-based file connectors (Sprint G).
    "S3Config",
    "S3FileSink",
    "S3FileSource",
    "SFTPConfig",
    "SFTPFileSink",
    "SFTPFileSource",
]

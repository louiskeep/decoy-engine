"""Public SDK surface for third-party Decoy connector authors.

`FileSource` and `FileSink` are the two abstract base classes a connector
inherits from. The engine discovers connectors via the `decoy.connectors`
setuptools entry point and instantiates them with a Pydantic-validated
config. Customers write their own cloud-storage connector in roughly 200
lines of Python and drop it in via `pip install`; the engine and HiFi UI
pick it up without engine-side changes.

Design notes (read before adding capabilities or breaking changes):

* `IOHandler` in `decoy_engine.connectors.base` is the legacy path-based
  abstraction that powers CSV / fixed-width / database handlers. It stays
  for backward-compat. `FileSource` / `FileSink` are NOT subclasses of
  `IOHandler`; the two shapes (`load_data() -> DataFrame` vs.
  `open(path) -> Iterator[bytes]`) are different enough that unifying them
  would muddy both. Keep them as peer abstractions.

* Capability flags are additive. Adding a new flag to this module must not
  break old connectors. Engines must default to False for unknown flags.

* `min_sdk_version` lets a connector require a newer SDK than the one the
  customer has installed. The engine checks this at connector-load time
  and refuses to load an incompatible connector with a clear admin-facing
  error rather than a runtime crash.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Generic, Iterator, Optional, TypeVar

from pydantic import BaseModel

__all__ = [
    "SDK_VERSION",
    # Capability flag constants.
    "CAP_STREAMING",
    "CAP_RESUMABLE",
    "CAP_SIGNED_URL",
    "CAP_MULTIPART",
    "CAP_INTROSPECTION",
    "CAP_DRY_RUN",
    # Config + value types.
    "ConnectorConfig",
    "FileMeta",
    "CheckResult",
    "WriteResult",
    # Abstract bases.
    "FileSource",
    "FileSink",
    # Exceptions.
    "ConnectorError",
    "TransientError",
    "PermanentError",
    "ConfigError",
]


SDK_VERSION = "1.0"
"""Public SDK contract version. Bumped on breaking changes to ABCs or types.

Connectors declare a `min_sdk_version` class attribute; the engine refuses
to load any connector whose `min_sdk_version` is newer than this constant.
"""


# Capability flag string constants. Connectors set these as keys in their
# `capabilities` class attribute (a dict[str, bool]) so the engine can pick
# the optimal code path without per-connector conditionals.
CAP_STREAMING = "supports_streaming"
CAP_RESUMABLE = "supports_resumable"
CAP_SIGNED_URL = "supports_signed_url"
CAP_MULTIPART = "supports_multipart"
CAP_INTROSPECTION = "supports_introspection"
CAP_DRY_RUN = "supports_dry_run"


class ConnectorConfig(BaseModel):
    """Pydantic base for connector configuration models.

    Each connector subclasses this and declares fields. The HiFi UI auto-
    renders the config form from `Subclass.model_json_schema()`. Use
    `pydantic.SecretStr` for credentials; these are auto-redacted in log
    capture and in the audit chain.

    Variable interpolation (`${var.X}` / `${env.X}`) happens before config
    validation, so a connector sees fully-resolved values.
    """

    model_config = {"extra": "forbid"}


@dataclass(frozen=True)
class FileMeta:
    """Lightweight metadata for one file in a `FileSource`.

    Sources that can't cheaply provide `size` / `content_type` / `modified`
    leave them None. The engine treats None as "unknown" and falls back to
    on-demand HEAD requests when it needs the data.
    """

    path: str
    size: Optional[int] = None
    content_type: Optional[str] = None
    modified: Optional[str] = None  # ISO-8601, or None.


@dataclass(frozen=True)
class CheckResult:
    """Outcome of `FileSource.check()` / `FileSink.check()`.

    `ok=True` means the connector reached the configured location and has
    sufficient permission. `ok=False` with `detail` is for runtime-side
    issues (network, expired credentials, missing bucket); for config-shape
    errors raise `ConfigError` instead so HiFi can surface a red banner on
    the form rather than starting the job.
    """

    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a successful `FileSink.write()` call."""

    path: str
    bytes_written: int


# ----- Exceptions ---------------------------------------------------------
#
# SDK errors compose into the existing `decoy_engine.exceptions` hierarchy
# so engine-level handlers that already `except ConnectorError` keep working
# across the boundary. `ConfigError` deliberately stays under `DecoyError`,
# not under `ConnectorError`: a bad config is a fix-the-form problem, not
# a fix-the-network problem, and shouldn't be caught by retry logic.

from decoy_engine.exceptions import (
    ConfigError,
    ConnectorError,
)


class TransientError(ConnectorError):
    """Temporary failure: rate-limit, transient network blip, 5xx from upstream.

    The engine retries with exponential backoff (defaults: 3 attempts, 1s
    base, 2x backoff, +/- 25% jitter).
    """


class PermanentError(ConnectorError):
    """Non-recoverable connector failure. Fails the run immediately, no retry.

    Distinct from `ConfigError`: this is a runtime failure (object gone,
    quota exceeded mid-write, etc.) that the user resolves by adjusting
    state in the remote system, not by editing their config.
    """


# ----- Abstract base classes ---------------------------------------------

ConfigT = TypeVar("ConfigT", bound=ConnectorConfig)


class _ConnectorBase(Generic[ConfigT], ABC):
    """Shared metadata, init contract, and lifecycle hook.

    Internal: customers inherit from `FileSource` / `FileSink`, not from
    this class directly. The split between source and sink keeps the
    method surface tight and lets a connector implement only the side it
    cares about.
    """

    name: ClassVar[str] = ""
    """Stable identifier, e.g. "s3", "gcs", "sftp". Snake case, lowercase."""

    version: ClassVar[str] = ""
    """Connector implementation version. Semver; bumped on behavior change."""

    min_sdk_version: ClassVar[str] = SDK_VERSION
    """Lowest SDK version this connector supports. Engine refuses to load
    if installed SDK is older.
    """

    capabilities: ClassVar[dict[str, bool]] = {}
    """Map of capability flag (one of the CAP_* constants) to whether the
    connector implements it. Missing keys default to False at the engine.
    """

    def __init__(self, config: ConfigT) -> None:
        self.config = config

    @abstractmethod
    def check(self) -> CheckResult:
        """Verify the configured location is reachable + permissioned.

        Called once before any read/write begins. Cheap as possible: a HEAD
        request or a single LIST with limit=1 is the right shape.

        Raise `ConfigError` if the config itself is wrong. Return
        `CheckResult(ok=False, detail=...)` for runtime issues that the
        user can fix by retrying or fixing their environment.
        """

    def close(self) -> None:
        """Optional cleanup hook for connection pools, open sockets, etc.

        Default is a no-op. Engine calls this in a finally block after the
        last read/write, even on error.
        """
        return None


class FileSource(_ConnectorBase[ConfigT]):
    """Read files from a remote location.

    First-party implementations: `S3FileSource` (`decoy_engine.connectors.s3`),
    `GCSFileSource` (`decoy_engine.connectors.gcs`), `SFTPFileSource`
    (`decoy_engine.connectors.sftp`). Community connectors live in their own
    packages and ship via the `decoy.connectors` entry point.
    """

    @abstractmethod
    def list(self, prefix: Optional[str] = None) -> Iterator[FileMeta]:
        """Yield metadata for files under `prefix`.

        Empty `prefix` (or None) lists everything the connector's config
        scope grants access to. Implementations should yield lazily so
        callers can short-circuit on large prefixes.
        """

    @abstractmethod
    def open(self, path: str) -> Iterator[bytes]:
        """Yield streaming byte chunks for `path`.

        Implementations should pick chunk size to amortize per-chunk
        overhead while bounding RSS. Roughly 1 MB is a reasonable default;
        S3 multipart minimum is 5 MB if a sink expects to round-trip into
        a multipart upload without buffering.

        Raise `PermanentError` if the path is missing or unauthorized;
        raise `TransientError` for retryable failures.
        """


class FileSink(_ConnectorBase[ConfigT]):
    """Write files to a remote location.

    Symmetric to `FileSource`. A connector can implement both if the
    underlying service supports both directions; many will (S3, GCS, SFTP
    do).
    """

    @abstractmethod
    def write(self, path: str, chunks: Iterator[bytes]) -> WriteResult:
        """Write `chunks` to `path`, consuming the iterator.

        Implementations with `supports_multipart=True` should upload chunks
        concurrently where possible. Non-multipart sinks can buffer in
        memory or stream sequentially to a single PUT.

        Raise `PermanentError` for unauthorized / missing-bucket / quota
        errors; raise `TransientError` for retryable failures. `close()`
        is called by the engine after the last write; cleanup of any
        partial-upload state belongs there.
        """

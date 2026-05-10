"""Google Cloud Storage file source + sink built on the public connector SDK.

Uses google-cloud-storage. Optional extra: `pip install decoy-engine[gcs]`.

Auth modes (in order of preference):
* Service account JSON content as a string in `config.service_account_json`.
* Application Default Credentials (ADC) when no SA JSON is provided.
  ADC covers `GOOGLE_APPLICATION_CREDENTIALS`, `gcloud auth login`, GCE
  metadata server, GKE workload identity. Mirrors the AWS default-chain
  pattern.

GCS does not require an explicit endpoint override the way S3 does for
S3-compatibles. The library handles signed-URL generation and resumable
uploads internally; the SDK capabilities flag both.
"""
from __future__ import annotations

import json
from typing import ClassVar, Iterator, Optional

from pydantic import Field, SecretStr

from decoy_engine.sdk import (
    CAP_INTROSPECTION,
    CAP_MULTIPART,
    CAP_SIGNED_URL,
    CAP_STREAMING,
    CheckResult,
    ConnectorConfig,
    FileMeta,
    FileSink,
    FileSource,
    PermanentError,
    TransientError,
    WriteResult,
)

__all__ = ["GCSConfig", "GCSFileSource", "GCSFileSink"]


_DEFAULT_CHUNK_BYTES = 1 * 1024 * 1024


def _wrap_gcs_error(exc: Exception) -> Exception:
    """Translate google-cloud-storage exceptions into typed SDK errors.

    google.api_core.exceptions.NotFound -> PermanentError (404).
    Forbidden / PermissionDenied -> PermanentError.
    ServiceUnavailable / TooManyRequests / DeadlineExceeded -> TransientError.
    Anything else: assume permanent.
    """
    try:
        from google.api_core import exceptions as gax_exc
    except Exception:
        # google libraries not installed: shouldn't happen in tests that
        # exercise the connector, but be safe.
        return PermanentError(f"Unexpected GCS error: {exc}")

    if isinstance(exc, (gax_exc.NotFound,)):
        return PermanentError(f"GCS not found: {exc}")
    if isinstance(exc, (gax_exc.Forbidden, gax_exc.PermissionDenied, gax_exc.Unauthenticated)):
        return PermanentError(f"GCS permission denied: {exc}")
    if isinstance(
        exc,
        (
            gax_exc.ServiceUnavailable,
            gax_exc.TooManyRequests,
            gax_exc.DeadlineExceeded,
            gax_exc.GatewayTimeout,
            gax_exc.InternalServerError,
        ),
    ):
        return TransientError(f"GCS transient error: {exc}")
    if isinstance(exc, gax_exc.BadRequest):
        return PermanentError(f"GCS bad request: {exc}")
    return PermanentError(f"Unexpected GCS error: {exc}")


class GCSConfig(ConnectorConfig):
    """Config for GCS source/sink.

    `service_account_json` is the JSON service-account key content (the
    whole file, not just the path). When None, the SDK falls back to
    Application Default Credentials.
    """

    bucket: str = Field(..., min_length=1, max_length=255)
    prefix: str = ""
    project: Optional[str] = None
    service_account_json: Optional[SecretStr] = Field(
        default=None,
        description=(
            "JSON-encoded service account key content. When unset the SDK "
            "uses Application Default Credentials: env var "
            "GOOGLE_APPLICATION_CREDENTIALS, then gcloud user creds, then "
            "GCE metadata service. Matches AWS default-chain semantics."
        ),
    )


def _build_gcs_client(config: GCSConfig):
    """Construct a google.cloud.storage.Client from the config.

    Uses service-account creds when provided; otherwise lets the library
    discover ADC.
    """
    from google.cloud import storage

    if config.service_account_json is not None:
        from google.oauth2 import service_account

        sa_info = json.loads(config.service_account_json.get_secret_value())
        credentials = service_account.Credentials.from_service_account_info(sa_info)
        return storage.Client(project=config.project, credentials=credentials)
    return storage.Client(project=config.project)


def _join_key(prefix: str, path: str) -> str:
    """Compose `prefix/path` the same way the S3 connector does."""
    p = (prefix or "").rstrip("/")
    k = (path or "").lstrip("/")
    return f"{p}/{k}" if p else k


class GCSFileSource(FileSource[GCSConfig]):
    """Read files from a GCS bucket."""

    name: ClassVar[str] = "gcs"
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
        CAP_SIGNED_URL: True,
        CAP_INTROSPECTION: True,
    }

    def __init__(self, config: GCSConfig) -> None:
        super().__init__(config)
        self._client = None
        self._bucket = None

    def _bucket_or_build(self):
        if self._bucket is None:
            self._client = _build_gcs_client(self.config)
            self._bucket = self._client.bucket(self.config.bucket)
        return self._bucket

    def check(self) -> CheckResult:
        try:
            self._client = _build_gcs_client(self.config)
            # `get_bucket` performs an actual GET; raises NotFound if the
            # bucket is missing or unauthorized.
            self._client.get_bucket(self.config.bucket)
            self._bucket = self._client.bucket(self.config.bucket)
        except Exception as exc:
            return CheckResult(ok=False, detail=str(_wrap_gcs_error(exc)))
        return CheckResult(ok=True)

    def list(self, prefix: Optional[str] = None) -> Iterator[FileMeta]:
        bucket = self._bucket_or_build()
        effective_prefix = _join_key(self.config.prefix, prefix or "")
        try:
            for blob in bucket.list_blobs(prefix=effective_prefix or None):
                yield FileMeta(
                    path=blob.name,
                    size=blob.size,
                    content_type=blob.content_type,
                    modified=blob.updated.isoformat() if blob.updated else None,
                )
        except Exception as exc:
            raise _wrap_gcs_error(exc) from exc

    def head(self, path: str) -> FileMeta:
        bucket = self._bucket_or_build()
        blob = bucket.blob(path)
        try:
            blob.reload()
        except Exception as exc:
            raise _wrap_gcs_error(exc) from exc
        return FileMeta(
            path=path,
            size=blob.size,
            content_type=blob.content_type,
            modified=blob.updated.isoformat() if blob.updated else None,
        )

    def open(self, path: str) -> Iterator[bytes]:
        bucket = self._bucket_or_build()
        blob = bucket.blob(path)
        try:
            # Blob.open returns a file-like wrapping an HTTPS stream;
            # iterate in chunks for bounded memory use.
            with blob.open("rb") as remote:
                while True:
                    chunk = remote.read(_DEFAULT_CHUNK_BYTES)
                    if not chunk:
                        return
                    yield chunk
        except Exception as exc:
            raise _wrap_gcs_error(exc) from exc


class GCSFileSink(FileSink[GCSConfig]):
    """Write files to a GCS bucket."""

    name: ClassVar[str] = "gcs"
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
        # google-cloud-storage auto-switches to resumable multipart for
        # writes above 8 MiB. The SDK surfaces this as a single "multipart"
        # capability rather than a separate "resumable" flag.
        CAP_MULTIPART: True,
        CAP_SIGNED_URL: True,
    }

    def __init__(self, config: GCSConfig) -> None:
        super().__init__(config)
        self._client = None
        self._bucket = None

    def _bucket_or_build(self):
        if self._bucket is None:
            self._client = _build_gcs_client(self.config)
            self._bucket = self._client.bucket(self.config.bucket)
        return self._bucket

    def check(self) -> CheckResult:
        try:
            self._client = _build_gcs_client(self.config)
            self._client.get_bucket(self.config.bucket)
            self._bucket = self._client.bucket(self.config.bucket)
        except Exception as exc:
            return CheckResult(ok=False, detail=str(_wrap_gcs_error(exc)))
        return CheckResult(ok=True)

    def write(self, path: str, chunks: Iterator[bytes]) -> WriteResult:
        bucket = self._bucket_or_build()
        key = _join_key(self.config.prefix, path)
        blob = bucket.blob(key)

        bytes_written = 0
        try:
            # google-cloud-storage's Blob.open("wb") streams the upload
            # and handles resumable session negotiation internally for
            # large writes. We don't need to manage parts ourselves.
            with blob.open("wb") as remote:
                for chunk in chunks:
                    if not chunk:
                        continue
                    remote.write(chunk)
                    bytes_written += len(chunk)
        except Exception as exc:
            raise _wrap_gcs_error(exc) from exc

        return WriteResult(path=key, bytes_written=bytes_written)

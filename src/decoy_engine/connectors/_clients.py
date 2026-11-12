"""One place for Google Cloud Storage clients, call bounds and failure text.

Every GCS client the engine builds comes from ``gcs_client`` and every GCS call goes
through one of the ``gcs_*`` wrappers, so each call carries an engine-owned
``timeout=`` and ``retry=``, and every failure leaves with fixed text chosen by
exception type (``gcs_error``). A hung or black-holed endpoint therefore ends a job
or profile read inside a bound the engine sets, and no SDK message, bucket, object,
host or URL reaches ``str()`` of a raised error or ``CheckResult.detail``.

Pattern: the per-call ``timeout`` (requests ``(connect, read)`` tuple) and
``retry`` (google-api-core ``Retry``, total bound via ``Retry.with_timeout``) knobs
google-cloud-storage documents in "Configuring Timeouts and Retries"
(https://cloud.google.com/python/docs/reference/storage/latest/retry_timeout);
``storage.Client`` itself has no timeout parameter. The connect and read seconds
match the botocore ``Config(connect_timeout=5, read_timeout=60)`` the S3 clients use,
and the retry total equals the library's own default deadline.

Named limit: the outbound sentry (``tests/sentry/test_outbound_clients.py``) matches
calls by method name, so it does not see a bound method stored in a variable and
called later (``download = blob.download_as_bytes; download()``), nor a client built
inside a third-party library the engine does not wrap, nor calls wrapped in
``functools.partial`` or made through ``google.cloud.storage.transfer_manager``. It scans ``src/decoy_engine``
only; ``scripts/`` and ``testflight/`` are out of scope.

This module holds no file APIs and has no file-access registry row; keep it so.
Google imports are local so ``import decoy_engine`` works without the cloud extra.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from decoy_engine.sdk import ConnectorError, PermanentError, TransientError

# Read at call time, never bound into defaults, so tests can shorten them.
GCS_TIMEOUT: tuple[float, float] = (5.0, 60.0)
GCS_RETRY_TOTAL_S: float = 120.0

_NOT_FOUND = "GCS bucket or object not found"
_DENIED = "GCS permission denied"
_TRANSIENT = "GCS transient network or service error"
_REJECTED = "GCS rejected the request"
_CREDENTIALS = "GCS credentials unavailable or invalid"
_UNEXPECTED = "Unexpected GCS error"


def gcs_retry() -> Any:
    """The library's default retry policy, bounded to ``GCS_RETRY_TOTAL_S`` in total."""
    try:
        from google.cloud.storage.retry import DEFAULT_RETRY
    except ImportError:
        # Only reachable with a stand-in client (no library means no real call).
        return None
    return DEFAULT_RETRY.with_timeout(GCS_RETRY_TOTAL_S)


def gcs_error(exc: BaseException) -> ConnectorError:
    """Classify a GCS failure into a typed SDK error with fixed, value-free text.

    Exact-type table checked with ``isinstance``; the original survives only as
    ``__cause__`` (a server-admin surface), never in the text.
    """
    if isinstance(exc, (TransientError, PermanentError)):
        return exc
    try:
        from google.api_core import exceptions as gax
        from google.auth import exceptions as gauth
        from requests import exceptions as req
    except ImportError:
        return PermanentError(_UNEXPECTED)
    # TransportError and TimeoutError subclass GoogleAuthError: check them first.
    if isinstance(exc, (gauth.TransportError, gauth.TimeoutError)):
        return TransientError(_TRANSIENT)
    if isinstance(exc, gauth.GoogleAuthError):
        return PermanentError(_CREDENTIALS)
    if isinstance(exc, gax.NotFound):
        return PermanentError(_NOT_FOUND)
    if isinstance(exc, (gax.Forbidden, gax.PermissionDenied, gax.Unauthenticated)):
        return PermanentError(_DENIED)
    if isinstance(
        exc,
        (
            gax.ServiceUnavailable,
            gax.TooManyRequests,
            gax.DeadlineExceeded,
            gax.GatewayTimeout,
            gax.InternalServerError,
            gax.RetryError,
            req.Timeout,
            req.ConnectionError,
        ),
    ):
        return TransientError(_TRANSIENT)
    if isinstance(exc, gax.BadRequest):
        return PermanentError(_REJECTED)
    return PermanentError(_UNEXPECTED)


def _guard(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one GCS call; a failure leaves as ``gcs_error(exc)`` chained to ``exc``."""
    try:
        return call(*args, **kwargs)
    except Exception as exc:
        err = gcs_error(exc)
        if err is exc:
            raise
        raise err from exc


def gcs_client(**kwargs: Any) -> Any:
    """``storage.Client(**kwargs)``, forwarding only what the caller passed.

    ``Client()`` infers the project from the environment; ``Client(project=None)``
    means no project. Callers keep whichever they used.
    """

    def build() -> Any:
        from google.cloud import storage

        return storage.Client(**kwargs)

    return _guard(build)


def gcs_service_account(info_text: str) -> Any:
    """Credentials from service-account JSON text, failing with fixed text.

    The error is raised after the ``except`` block, so neither ``__cause__`` nor
    ``__context__`` holds the original: a ``JSONDecodeError`` keeps the whole input
    (the secret) in ``doc``.
    """
    try:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_info(json.loads(info_text))
    except Exception:
        pass
    raise PermanentError("GCS service account credentials could not be parsed")


def gcs_get_bucket(client: Any, name: str) -> Any:
    return _guard(client.get_bucket, name, timeout=GCS_TIMEOUT, retry=gcs_retry())


def gcs_list_blobs(bucket: Any, *, prefix: str | None) -> Iterator[Any]:
    """Iterate blobs; errors raised while fetching later pages are translated too."""
    pages = _guard(bucket.list_blobs, prefix=prefix, timeout=GCS_TIMEOUT, retry=gcs_retry())
    iterator, done = iter(pages), object()
    while (item := _guard(next, iterator, done)) is not done:
        yield item


def gcs_reload(blob: Any) -> None:
    _guard(blob.reload, timeout=GCS_TIMEOUT, retry=gcs_retry())


def gcs_download(blob: Any, *, start: int | None = None, end: int | None = None) -> bytes:
    return _guard(
        blob.download_as_bytes, start=start, end=end, timeout=GCS_TIMEOUT, retry=gcs_retry()
    )


class _TranslatingFile:
    """The object ``Blob.open`` returns, with each I/O failure translated.

    Reads and writes on a ``BlobReader``/``BlobWriter`` are the network calls, so
    translation at ``open`` alone would let their errors through.
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def read(self, size: int = -1) -> bytes:
        return _guard(self._raw.read, size)

    def write(self, data: bytes) -> int:
        return _guard(self._raw.write, data)

    def close(self) -> None:
        _guard(self._raw.close)

    def __enter__(self) -> _TranslatingFile:
        _guard(self._raw.__enter__)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return _guard(self._raw.__exit__, exc_type, exc, tb)


def gcs_open(blob: Any, mode: str) -> _TranslatingFile:
    return _TranslatingFile(_guard(blob.open, mode, timeout=GCS_TIMEOUT, retry=gcs_retry()))


def s3_client(
    *,
    region: str | None,
    endpoint_url: str | None,
    access_key_id: str | None,
    secret_access_key: str | None,
) -> Any:
    """STUB (red-first): legacy factory."""
    import boto3
    from botocore.config import Config as BotoConfig

    kwargs: dict[str, Any] = {
        "region_name": region,
        "endpoint_url": endpoint_url,
        "config": BotoConfig(
            connect_timeout=5, read_timeout=60, retries={"max_attempts": 1, "mode": "standard"}
        ),
    }
    return boto3.client("s3", **kwargs)


def s3_error(exc: BaseException) -> ConnectorError:
    """STUB (red-first)."""
    return PermanentError("S3 stub")


def s3_call(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """STUB (red-first)."""
    return call(*args, **kwargs)

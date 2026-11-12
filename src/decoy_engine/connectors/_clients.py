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
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from decoy_engine.errors import ConnectorErrorKind
from decoy_engine.sdk import ConnectorError, PermanentError, TransientError

# Read at call time, never bound into defaults, so tests can shorten them.
GCS_TIMEOUT: tuple[float, float] = (5.0, 60.0)
GCS_RETRY_TOTAL_S: float = 120.0

# S3 multipart minimum part size. AWS rejects parts smaller than 5 MiB except the last.
_MULTIPART_MIN_PART_BYTES = 5 * 1024 * 1024

# Fixed words per kind. SDK text is untrusted and never joins a message.
_PHRASE: dict[ConnectorErrorKind, str] = {
    "not_found": "bucket or object not found",
    "access_denied": "permission denied",
    "transient": "transient network or service error",
    "rejected": "rejected the request",
    "credentials": "credentials unavailable or invalid",
    "precondition_failed": "precondition failed",
    "conflict": "conflicting write",
    "wrong_region": "bucket is in another region",
    "pointer_invalid": "pointer object is invalid",
    "unexpected": "unexpected error",
}


def _classified(provider: str, kind: ConnectorErrorKind) -> ConnectorError:
    """The one kind-to-class table: only ``transient`` is retried, every other kind is final."""
    text = f"Unexpected {provider} error" if kind == "unexpected" else f"{provider} {_PHRASE[kind]}"
    cls = TransientError if kind == "transient" else PermanentError
    return cls(text, kind=kind)


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

    Exact-type table checked with ``isinstance``; the original is not kept, so neither
    ``__cause__`` nor ``__context__`` can carry SDK text.
    """
    if isinstance(exc, (TransientError, PermanentError)):
        return exc
    return _classified("GCS", _gcs_kind(exc))


def _gcs_kind(exc: BaseException) -> ConnectorErrorKind:
    try:
        from google.api_core import exceptions as gax
        from google.auth import exceptions as gauth
        from requests import exceptions as req
    except ImportError:
        return "unexpected"
    # TransportError and TimeoutError subclass GoogleAuthError: check them first.
    if isinstance(exc, (gauth.TransportError, gauth.TimeoutError)):
        return "transient"
    if isinstance(exc, gauth.GoogleAuthError):
        return "credentials"
    if isinstance(exc, gax.NotFound):
        return "not_found"
    if isinstance(exc, (gax.Forbidden, gax.PermissionDenied, gax.Unauthenticated)):
        return "access_denied"
    if isinstance(exc, gax.PreconditionFailed):
        return "precondition_failed"
    if isinstance(exc, gax.Conflict):
        return "conflict"
    transient = (
        gax.ServiceUnavailable,
        gax.TooManyRequests,
        gax.DeadlineExceeded,
        gax.GatewayTimeout,
        gax.InternalServerError,
        gax.RetryError,
        req.Timeout,
        req.ConnectionError,
    )
    if isinstance(exc, transient):
        return "transient"
    return "rejected" if isinstance(exc, gax.BadRequest) else "unexpected"


def _call(
    classify: Callable[[BaseException], ConnectorError],
    call: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run ``call``; a failure leaves as ``classify(exc)``, raised after the ``except`` block.

    Raising outside the handler keeps ``__cause__`` and ``__context__`` empty, so the SDK
    error (and any text it holds) is unreachable from the raised one.
    """
    try:
        return call(*args, **kwargs)
    except Exception as exc:
        err = classify(exc)
        if err is exc:
            raise
    raise err


def _guard(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one GCS call; a failure leaves as ``gcs_error(exc)`` with no chain."""
    return _call(gcs_error, call, *args, **kwargs)


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


# S3 error codes (or bare HTTP statuses, which head calls report as the code) to kinds.
_S3_KINDS: dict[str, ConnectorErrorKind] = {
    **dict.fromkeys(("NoSuchBucket", "NoSuchKey", "404", "NotFound"), "not_found"),
    **dict.fromkeys(("403", "AccessDenied", "AllAccessDisabled"), "access_denied"),
    **dict.fromkeys(("InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken"), "credentials"),
    **dict.fromkeys(("PreconditionFailed", "412"), "precondition_failed"),
    **dict.fromkeys(("ConditionalRequestConflict", "409"), "conflict"),
    **dict.fromkeys(("PermanentRedirect", "301", "AuthorizationHeaderMalformed"), "wrong_region"),
    # The codes botocore's standard retry mode retries (retries/standard.py throttled and
    # transient codes, 429 and 509 in _retry.json) are transient for the caller.
    **dict.fromkeys(
        (
            "SlowDown",
            "InternalError",
            "ServiceUnavailable",
            "RequestTimeout",
            "RequestTimeoutException",
            "PriorRequestNotComplete",
            "Throttling",
            "ThrottlingException",
            "ThrottledException",
            "RequestThrottledException",
            "TooManyRequests",
            "TooManyRequestsException",
            "ProvisionedThroughputExceededException",
            "TransactionInProgressException",
            "RequestLimitExceeded",
            "BandwidthLimitExceeded",
            "LimitExceededException",
            "RequestThrottled",
            "EC2ThrottledException",
            "429",
            "509",
        ),
        "transient",
    ),
}


def s3_client(
    *,
    region: str | None,
    endpoint_url: str | None,
    access_key_id: str | None,
    secret_access_key: str | None,
    single_request: bool,
) -> Any:
    """The one S3 client factory: bounded timeouts and an explicit attempt count.

    An ordinary client lets the SDK retry a request once (two attempts, as before). A
    ``single_request`` client never resends, for conditional writes where a resend can turn
    a win into a loss. The engine has no retry loop of its own. With no region botocore
    resolves one from the environment or profile. Static credentials are passed only when
    set, so the default chain stays in force otherwise.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    kwargs: dict[str, Any] = {
        "endpoint_url": endpoint_url,
        "config": BotoConfig(
            connect_timeout=5,
            read_timeout=60,
            retries={"total_max_attempts": 1 if single_request else 2, "mode": "standard"},
        ),
    }
    if region is not None:
        kwargs["region_name"] = region
    if access_key_id is not None:
        kwargs["aws_access_key_id"] = access_key_id
    if secret_access_key is not None:
        kwargs["aws_secret_access_key"] = secret_access_key
    # A bad endpoint, half-set credentials or an unknown profile fail here, not on first use.
    return _call(s3_error, boto3.client, "s3", **kwargs)


def _s3_kind(exc: BaseException) -> ConnectorErrorKind:
    try:
        from botocore import exceptions as bce
    except ImportError:
        return "unexpected"
    # BotoCoreError has no credentials base class, so the sibling families are listed.
    if isinstance(
        exc,
        (
            bce.NoCredentialsError,
            bce.PartialCredentialsError,
            bce.CredentialRetrievalError,
            bce.TokenRetrievalError,
            bce.SSOError,
        ),
    ):
        return "credentials"
    if isinstance(exc, (bce.ConnectionError, bce.HTTPClientError)):
        return "transient"
    if not isinstance(exc, bce.ClientError):
        return "unexpected"
    response = exc.response if isinstance(exc.response, dict) else {}
    code = (response.get("Error") or {}).get("Code")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    if isinstance(code, str) and code in _S3_KINDS:
        return _S3_KINDS[code]
    if not isinstance(status, int) or isinstance(status, bool):
        return "unexpected"
    if str(status) in _S3_KINDS:
        return _S3_KINDS[str(status)]
    if status >= 500:
        return "transient"
    return "rejected" if status >= 400 else "unexpected"


def s3_error(exc: BaseException) -> ConnectorError:
    """Classify an S3 failure into a typed SDK error with fixed text and a ``kind``.

    No SDK message, ARN, bucket or key is read into the result.
    """
    if isinstance(exc, (TransientError, PermanentError)):
        return exc
    return _classified("S3", _s3_kind(exc))


def s3_call(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one S3 SDK call; a failure leaves as ``s3_error(exc)`` with no chain."""
    return _call(s3_error, call, *args, **kwargs)


def s3_put(
    client: Any, bucket: str, key: str, chunks: Iterator[bytes], conditions: Mapping[str, str]
) -> int:
    """Write ``chunks`` to one object and return the bytes written: the one S3 put path.

    Bodies under the 5 MiB part minimum go as a single ``put_object``; larger ones as a
    multipart upload of exact 5 MiB parts plus a short final part. ``conditions`` (for
    example ``{"IfNoneMatch": "*"}``) ride on the call that creates the object, the single
    PUT or ``complete_multipart_upload``. A failure after the upload starts aborts it, best
    effort, so an abort error never masks the write error.
    """
    buffer = bytearray()
    upload_id: str | None = None
    parts: list[dict[str, Any]] = []
    total = 0

    def upload(payload: bytes) -> None:
        nonlocal total
        number = len(parts) + 1
        response = s3_call(
            client.upload_part,
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=number,
            Body=payload,
        )
        parts.append({"PartNumber": number, "ETag": response["ETag"]})
        total += len(payload)

    try:
        for chunk in chunks:
            buffer.extend(chunk)
            while len(buffer) >= _MULTIPART_MIN_PART_BYTES:
                if upload_id is None:
                    started = s3_call(client.create_multipart_upload, Bucket=bucket, Key=key)
                    upload_id = started["UploadId"]
                upload(bytes(buffer[:_MULTIPART_MIN_PART_BYTES]))
                del buffer[:_MULTIPART_MIN_PART_BYTES]
        if upload_id is None:
            s3_call(client.put_object, Bucket=bucket, Key=key, Body=bytes(buffer), **conditions)
            return len(buffer)
        if buffer:
            upload(bytes(buffer))
        s3_call(
            client.complete_multipart_upload,
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
            **conditions,
        )
        return total
    except Exception:
        if upload_id is not None:
            try:
                client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            except Exception:
                pass
        raise

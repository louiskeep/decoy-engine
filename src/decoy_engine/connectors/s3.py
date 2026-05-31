"""S3 file source + sink built on the public connector SDK.

This is the first-party reference implementation of `FileSource` /
`FileSink`. It targets real AWS S3 and any S3-compatible service that
accepts a custom `endpoint_url` (MinIO, Cloudflare R2, Wasabi, Backblaze
B2, etc.).

Streaming semantics:

* `S3FileSource.open()` yields ~1 MB chunks via `StreamingBody.iter_chunks`.
  Memory use is bounded regardless of object size.
* `S3FileSink.write()` accumulates input chunks into a 5 MB buffer (the
  S3 multipart minimum part size). Once the buffer reaches 5 MB the sink
  switches to a multipart upload and uploads parts as further chunks
  arrive. Files smaller than 5 MB are written via a single `put_object`
  call.

Error mapping:

* `botocore` `NoSuchBucket` / `NoSuchKey` / 404 / 403 are surfaced as
  `PermanentError` so the engine doesn't retry them.
* Networking errors (`EndpointConnectionError`, transient 5xx) raise
  `TransientError` so the engine's retry logic kicks in.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from pydantic import Field, SecretStr

from decoy_engine.sdk import (
    CAP_INTROSPECTION,
    CAP_MULTIPART,
    CAP_RESUMABLE,
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

__all__ = ["S3Config", "S3FileSink", "S3FileSource"]


# S3 multipart minimum part size. AWS rejects parts smaller than 5 MiB
# except for the final part of a multipart upload.
_MULTIPART_MIN_PART_BYTES = 5 * 1024 * 1024

# Default streaming read chunk size for `open()`. ~1 MiB amortizes per-chunk
# overhead without blowing past the platform's smallest worker RSS budget.
_DEFAULT_READ_CHUNK_BYTES = 1 * 1024 * 1024


# Error codes from botocore that indicate a permanent (do-not-retry) failure.
# Anything else falls into the transient bucket so the engine's retry policy
# decides whether to back off and try again.
_PERMANENT_S3_ERROR_CODES = frozenset(
    {
        "NoSuchBucket",
        "NoSuchKey",
        "404",
        "403",
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "AllAccessDisabled",
    }
)


def _wrap_client_error(exc: Exception) -> Exception:
    """Translate a botocore ClientError into a typed SDK error.

    Permanent codes (bucket/key missing, auth) raise `PermanentError`.
    Transient network conditions (endpoint unreachable, connect timeout,
    read timeout) raise `TransientError` so the engine retries.

    QA 2026-05-31 session2 F1 (HIGH) closure: ConnectTimeoutError +
    ReadTimeoutError are subclasses of BotoCoreError -- NOT
    EndpointConnectionError or ClientError -- so the prior code
    classified them as ``PermanentError`` and immediately aborted jobs
    that could have succeeded on retry. Now they fall through the
    transient branch.
    """
    from botocore.exceptions import (
        ClientError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ReadTimeoutError,
    )

    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)):
        return TransientError(f"S3 transient connection error: {exc}")
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _PERMANENT_S3_ERROR_CODES:
            return PermanentError(f"S3 {code}: {exc.response.get('Error', {}).get('Message', '')}")
        return TransientError(f"S3 transient error {code}: {exc}")
    # Anything else: assume permanent so callers don't loop on programmer error.
    return PermanentError(f"Unexpected S3 error: {exc}")


class S3Config(ConnectorConfig):
    """Config shared by `S3FileSource` and `S3FileSink`.

    The same config powers both directions because the AWS SDK uses one
    client per service regardless of read/write. Customers with separate
    read-only and write-only IAM roles instantiate two connectors with
    different configs.
    """

    bucket: str = Field(..., min_length=1, max_length=255)
    prefix: str = ""
    region: str = "us-east-1"
    # Credentials are optional so the SDK supports the AWS default-chain
    # auth modes that real shops use: EC2 instance profile, ECS task role,
    # EKS IRSA, IAM Identity Center, env vars (AWS_ACCESS_KEY_ID etc.),
    # `~/.aws/credentials`. When both are None, boto3 walks the chain.
    # When provided, they take precedence over the chain.
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    endpoint_url: str | None = Field(
        default=None,
        description=(
            "Override the AWS endpoint, e.g. https://<account>.r2.cloudflarestorage.com "
            "for Cloudflare R2 or http://localhost:9000 for a MinIO dev server. "
            "Leave None for real AWS S3."
        ),
    )


def _build_s3_client(config: S3Config):
    """Construct a boto3 client with bounded timeouts.

    Modest timeouts so a misconfigured endpoint doesn't hang for 10 minutes
    waiting on TCP. The engine's retry logic provides the backoff layer
    above; we don't double up here.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    # Only pass static creds when explicitly configured. Passing None
    # would override the default chain with "no credentials" and cause
    # auth failures on hosts that rely on instance profiles or env vars.
    kwargs: dict = {
        "region_name": config.region,
        "endpoint_url": config.endpoint_url,
        "config": BotoConfig(
            connect_timeout=5,
            read_timeout=60,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    }
    if config.access_key_id is not None:
        kwargs["aws_access_key_id"] = config.access_key_id.get_secret_value()
    if config.secret_access_key is not None:
        kwargs["aws_secret_access_key"] = config.secret_access_key.get_secret_value()
    return boto3.client("s3", **kwargs)


def _join_key(prefix: str, path: str) -> str:
    """Compose `prefix/path` cleanly without leading/trailing-slash mishaps.

    Treats prefix `""` and missing trailing slash as identity. Caller-supplied
    `path` may start with `/`; we strip it so a configured prefix isn't
    silently bypassed.
    """
    p = (prefix or "").rstrip("/")
    k = (path or "").lstrip("/")
    return f"{p}/{k}" if p else k


class S3FileSource(FileSource[S3Config]):
    """Read files from an S3 bucket (or S3-compatible service)."""

    name: ClassVar[str] = "s3"
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
        CAP_RESUMABLE: True,  # supported via Range header re-requests
        CAP_SIGNED_URL: True,  # via boto3 generate_presigned_url
        CAP_INTROSPECTION: True,  # list returns size + content_type
    }

    def __init__(self, config: S3Config) -> None:
        super().__init__(config)
        # Lazy: defer client construction until first call so that
        # constructing the source object is cheap and side-effect-free.
        self._client = None

    def _client_or_build(self):
        if self._client is None:
            self._client = _build_s3_client(self.config)
        return self._client

    def check(self) -> CheckResult:
        """Verify the bucket exists and the configured credentials reach it."""
        client = self._client_or_build()
        try:
            client.head_bucket(Bucket=self.config.bucket)
        except Exception as exc:
            return CheckResult(ok=False, detail=str(_wrap_client_error(exc)))
        return CheckResult(ok=True)

    def list(self, prefix: str | None = None) -> Iterator[FileMeta]:
        """Yield FileMeta for every object under prefix.

        Combines the config-level prefix with the call-level prefix (the
        config prefix scopes the source; the call prefix narrows further
        within that scope).
        """
        client = self._client_or_build()
        effective_prefix = _join_key(self.config.prefix, prefix or "")
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self.config.bucket,
                Prefix=effective_prefix,
            ):
                for item in page.get("Contents", []):
                    # `LastModified` is a datetime; ISO format for FileMeta.
                    modified = item.get("LastModified")
                    yield FileMeta(
                        path=item["Key"],
                        size=item.get("Size"),
                        content_type=None,  # list_objects doesn't return content-type
                        modified=modified.isoformat() if modified else None,
                    )
        except Exception as exc:
            raise _wrap_client_error(exc) from exc

    def head(self, path: str) -> FileMeta:
        """Native S3 HEAD: one RPC, returns size and content-type.

        Overrides the default list-walking implementation. `path` is
        interpreted as an absolute S3 key (no prefix joining), matching
        the semantics of `open()`.
        """
        client = self._client_or_build()
        try:
            response = client.head_object(Bucket=self.config.bucket, Key=path)
        except Exception as exc:
            raise _wrap_client_error(exc) from exc
        modified = response.get("LastModified")
        return FileMeta(
            path=path,
            size=response.get("ContentLength"),
            content_type=response.get("ContentType"),
            modified=modified.isoformat() if modified else None,
        )

    def open(self, path: str) -> Iterator[bytes]:
        """Stream object bytes in ~1 MB chunks.

        `path` is interpreted as an absolute S3 key (no prefix joining).
        This matches what `list()` returned: callers iterate list, pick
        a key, pass it to open. Re-joining would double-prefix.
        """
        client = self._client_or_build()
        try:
            response = client.get_object(Bucket=self.config.bucket, Key=path)
        except Exception as exc:
            raise _wrap_client_error(exc) from exc

        body = response["Body"]
        try:
            for chunk in body.iter_chunks(chunk_size=_DEFAULT_READ_CHUNK_BYTES):
                if chunk:
                    yield chunk
        finally:
            body.close()


class S3FileSink(FileSink[S3Config]):
    """Write files to an S3 bucket (or S3-compatible service).

    Sizes below 5 MB use a single `put_object`; larger writes transparently
    switch to a multipart upload. Failures during multipart trigger
    `abort_multipart_upload` so partial uploads don't accumulate.
    """

    name: ClassVar[str] = "s3"
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
        CAP_MULTIPART: True,
        CAP_SIGNED_URL: True,
    }

    def __init__(self, config: S3Config) -> None:
        super().__init__(config)
        self._client = None

    def _client_or_build(self):
        if self._client is None:
            self._client = _build_s3_client(self.config)
        return self._client

    def check(self) -> CheckResult:
        client = self._client_or_build()
        try:
            client.head_bucket(Bucket=self.config.bucket)
        except Exception as exc:
            return CheckResult(ok=False, detail=str(_wrap_client_error(exc)))
        return CheckResult(ok=True)

    def write(self, path: str, chunks: Iterator[bytes]) -> WriteResult:
        """Write `chunks` to `<config.prefix>/<path>` in the bucket.

        Strategy:

        1. Buffer incoming chunks up to `_MULTIPART_MIN_PART_BYTES` (5 MiB).
        2. If the iterator is exhausted before the buffer fills, do a
           single `put_object`. This is the cheap path for small files.
        3. Otherwise initiate a multipart upload, push the filled buffer
           as part 1, then keep filling and pushing parts.
        4. On the final part (after the iterator exhausts), upload
           whatever remains in the buffer (no 5-MiB minimum applies to
           the last part) and complete the multipart upload.
        5. Any error after step 3 triggers `abort_multipart_upload` to
           clean up the in-progress upload state on S3.
        """
        client = self._client_or_build()
        key = _join_key(self.config.prefix, path)

        buffer = bytearray()
        upload_id: str | None = None
        parts: list[dict] = []
        part_number = 0
        total_bytes = 0

        try:
            for chunk in chunks:
                if not chunk:
                    continue
                buffer.extend(chunk)
                while len(buffer) >= _MULTIPART_MIN_PART_BYTES:
                    if upload_id is None:
                        upload_id = self._initiate_multipart(client, key)
                    # Take exactly the minimum part size from the head of
                    # the buffer; bytes beyond stay queued for the next part
                    # (or the final flush below).
                    payload = bytes(buffer[:_MULTIPART_MIN_PART_BYTES])
                    del buffer[:_MULTIPART_MIN_PART_BYTES]
                    part_number += 1
                    etag = self._upload_part(client, key, upload_id, part_number, payload)
                    parts.append({"PartNumber": part_number, "ETag": etag})
                    total_bytes += len(payload)

            # Iterator exhausted. Flush the tail.
            if upload_id is None:
                # Whole body fits under the multipart threshold: single PUT.
                payload = bytes(buffer)
                try:
                    client.put_object(
                        Bucket=self.config.bucket,
                        Key=key,
                        Body=payload,
                    )
                except Exception as exc:
                    raise _wrap_client_error(exc) from exc
                total_bytes += len(payload)
            else:
                # Multipart in flight; upload remaining buffer as the
                # final part (S3 allows the last part to be < 5 MiB) and
                # complete. If buffer is empty here, the upload is
                # exactly N*5MiB and we just complete with the parts
                # we already have.
                if buffer:
                    payload = bytes(buffer)
                    part_number += 1
                    etag = self._upload_part(client, key, upload_id, part_number, payload)
                    parts.append({"PartNumber": part_number, "ETag": etag})
                    total_bytes += len(payload)
                self._complete_multipart(client, key, upload_id, parts)
        except Exception:
            # Abort to free the S3-side multipart state. Best-effort: an
            # abort failure shouldn't mask the original write failure.
            if upload_id is not None:
                try:
                    client.abort_multipart_upload(
                        Bucket=self.config.bucket,
                        Key=key,
                        UploadId=upload_id,
                    )
                except Exception:
                    pass
            raise

        return WriteResult(path=key, bytes_written=total_bytes)

    # ----- multipart helpers ---------------------------------------------

    def _initiate_multipart(self, client, key: str) -> str:
        try:
            response = client.create_multipart_upload(Bucket=self.config.bucket, Key=key)
        except Exception as exc:
            raise _wrap_client_error(exc) from exc
        return response["UploadId"]

    def _upload_part(
        self,
        client,
        key: str,
        upload_id: str,
        part_number: int,
        body: bytes,
    ) -> str:
        try:
            response = client.upload_part(
                Bucket=self.config.bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=body,
            )
        except Exception as exc:
            raise _wrap_client_error(exc) from exc
        return response["ETag"]

    def _complete_multipart(
        self,
        client,
        key: str,
        upload_id: str,
        parts: list[dict],
    ) -> None:
        try:
            client.complete_multipart_upload(
                Bucket=self.config.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception as exc:
            raise _wrap_client_error(exc) from exc

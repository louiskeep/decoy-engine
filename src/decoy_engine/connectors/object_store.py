"""Create-only and compare-and-swap writes over one bucket, plus the reads that go with them.

``S3ObjectStore`` is what a cloud run publishes through: ``put_new`` makes an object that
must not exist, ``put_if`` moves a small pointer object only when its version is still the
one the caller read, ``delete_created`` removes only the object this run made, and
``read_small`` reads a pointer without ever truncating a foreign object into a valid-looking
value. A conditional write is one HTTP request: its client has no SDK retry, and nothing here
resends, because a resend after an applied write turns a win into a loss. The caller decides
what an ``unknown`` result means.

Versions are opaque strings (the S3 ETag). Keys are full object keys; ``config.prefix`` is not
applied. The GCS store carries only ``iter_object``, the source spool; its writes are not
built. Every failure leaves as a classified ``ConnectorError`` with fixed text, built by
``_clients``, so no bucket, key, endpoint or request id reaches a message.

Pattern: S3 conditional writes (``If-None-Match: *``, ``If-Match``) as table formats use them
for put-if-absent commit files and pointer swaps.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, BinaryIO, Literal, Protocol

from decoy_engine.connectors import _clients
from decoy_engine.connectors.gcs import GCSConfig, _build_gcs_client, _stream_blob
from decoy_engine.connectors.s3 import S3Config, _build_s3_client, _stream_object
from decoy_engine.sdk import ConnectorError, PermanentError

Version = str
PutResult = Literal["won", "rejected", "conflict", "unknown"]

_CHUNK_BYTES = 1024 * 1024
# 412 and a missing key both mean the captured version is not the current one.
_PUT_RESULT: dict[str, PutResult] = {
    "precondition_failed": "rejected",
    "not_found": "rejected",
    "conflict": "conflict",
}


class ObjectStore(Protocol):
    def put_new(self, key: str, fd: BinaryIO) -> Version: ...
    def read_small(self, key: str, limit: int) -> tuple[bytes, Version] | None: ...
    def put_if(self, key: str, data: bytes, expected: Version | None) -> PutResult: ...
    def delete_created(self, key: str, version: Version) -> None: ...
    def iter_object(self, key: str) -> Iterator[bytes]: ...
    def check_region(self) -> None: ...


class _EtagCapture:
    """A client that keeps the ETag of the call that creates the object, for ``s3_put``."""

    etag: Version = ""

    def __init__(self, client: Any) -> None:
        self._client = client

    def put_object(self, **kwargs: Any) -> Any:
        return self._keep(self._client.put_object(**kwargs))

    def complete_multipart_upload(self, **kwargs: Any) -> Any:
        return self._keep(self._client.complete_multipart_upload(**kwargs))

    def _keep(self, response: Any) -> Any:
        self.etag = response["ETag"]
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class S3ObjectStore:
    def __init__(self, config: S3Config) -> None:
        self._config = config
        self._built: dict[bool, Any] = {}

    def _client(self, *, single_request: bool) -> Any:
        if single_request not in self._built:
            self._built[single_request] = _build_s3_client(
                self._config, single_request=single_request
            )
        return self._built[single_request]

    def put_new(self, key: str, fd: BinaryIO) -> Version:
        """Create ``key`` from ``fd``; an existing object is ``precondition_failed``."""
        client = _EtagCapture(self._client(single_request=True))
        chunks = iter(lambda: fd.read(_CHUNK_BYTES), b"")
        _clients.s3_put(client, self._config.bucket, key, chunks, {"IfNoneMatch": "*"})
        return client.etag

    def read_small(self, key: str, limit: int) -> tuple[bytes, Version] | None:
        """The object's bytes and version, None when absent, ``pointer_invalid`` past ``limit``."""
        client = self._client(single_request=False)
        try:
            response = _clients.s3_call(client.get_object, Bucket=self._config.bucket, Key=key)
        except ConnectorError as err:
            if err.kind != "not_found":
                raise
            return None
        body = response["Body"]
        data = b""
        try:
            # read(n) returns at most n bytes, so one call can stop short of a longer object.
            while len(data) <= limit and (
                part := _clients.s3_call(body.read, limit + 1 - len(data))
            ):
                data += part
        finally:
            body.close()
        if len(data) > limit:
            raise PermanentError("S3 pointer object is invalid", kind="pointer_invalid")
        return data, response["ETag"]

    def put_if(self, key: str, data: bytes, expected: Version | None) -> PutResult:
        """Write ``data`` if ``key`` is still at ``expected`` (None: still absent); one request."""
        client = self._client(single_request=True)
        condition = {"IfNoneMatch": "*"} if expected is None else {"IfMatch": expected}
        try:
            _clients.s3_call(
                client.put_object, Bucket=self._config.bucket, Key=key, Body=data, **condition
            )
        except ConnectorError as err:
            return _PUT_RESULT.get(err.kind, "unknown")
        return "won"

    def delete_created(self, key: str, version: Version) -> None:
        """Delete ``key`` only while it is still the object at ``version``."""
        client = self._client(single_request=True)
        _clients.s3_call(client.delete_object, Bucket=self._config.bucket, Key=key, IfMatch=version)

    def iter_object(self, key: str) -> Iterator[bytes]:
        """The S3 source's read loop, so a read fix lands in one place."""
        yield from _stream_object(self._client(single_request=False), self._config.bucket, key)

    def check_region(self) -> None:
        """Refuse a bucket whose own region header differs from the client's region."""
        client = self._client(single_request=False)
        response = _clients.s3_call(client.head_bucket, Bucket=self._config.bucket)
        actual = response["ResponseMetadata"]["HTTPHeaders"].get("x-amz-bucket-region")
        if actual is not None and actual != client.meta.region_name:
            raise PermanentError("S3 bucket is in another region", kind="wrong_region")


class GCSObjectStore:
    """Source spool only: GCS writes are not built."""

    def __init__(self, config: GCSConfig) -> None:
        self._config = config

    def iter_object(self, key: str) -> Iterator[bytes]:
        """The GCS source's read loop."""
        config = self._config
        yield from _stream_blob(lambda: _build_gcs_client(config).bucket(config.bucket), key)

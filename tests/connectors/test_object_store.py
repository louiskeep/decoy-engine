"""The S3 object store: create-only writes, conditional puts, bounded reads (R2, risk rows 3, 6, 7).

moto runs in process here (server mode is not in the lock). A before-send hook counts the
HTTP requests a store sends and can apply a write and then drop its response, which is the
only way to see whether the store resends.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import pytest

botocore_exc = pytest.importorskip("botocore.exceptions")
moto = pytest.importorskip("moto")

from decoy_engine.connectors import _clients  # noqa: E402
from decoy_engine.connectors.object_store import GCSObjectStore, S3ObjectStore  # noqa: E402
from decoy_engine.connectors.s3 import S3Config  # noqa: E402
from decoy_engine.sdk import ConnectorError  # noqa: E402

BUCKET = "zz-marker-bkt"
KEY = "zz_marker_key/obj"
PART = 5 * 1024 * 1024


class Wire:
    """Counts requests; when armed, applies the next write in moto and drops its response."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.lose_next_put = False
        self.answer: tuple[int, str] | None = None

    def hook(self, request: Any, **_: Any) -> Any:
        from botocore.awsrequest import AWSResponse
        from moto.core.botocore_stubber import MockRawResponse
        from moto.core.models import botocore_stubber

        self.sent.append(request.method)
        if self.lose_next_put and request.method == "PUT":
            self.lose_next_put = False
            botocore_stubber("before-send", request)
            raise botocore_exc.ConnectionClosedError(endpoint_url="http://zz")
        if self.answer is not None:
            status, code = self.answer
            body = f"<Error><Code>{code}</Code></Error>".encode()
            return AWSResponse(request.url, status, {}, MockRawResponse(body))
        return None


@pytest.fixture
def wire() -> Wire:
    return Wire()


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch, wire: Wire) -> Iterator[Any]:
    """A bucket in us-east-1 and an unwired client; every store client gets the wire hook."""
    real = _clients.s3_client

    def wired(**kw: Any) -> Any:
        client = real(**kw)
        client.meta.events.register_first("before-send.s3", wire.hook)
        return client

    with moto.mock_aws():
        client = real(
            region="us-east-1",
            endpoint_url=None,
            access_key_id="k",
            secret_access_key="s",
            single_request=False,
        )
        client.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(_clients, "s3_client", wired)
        yield client


def _store(region: str | None = "us-east-1") -> S3ObjectStore:
    cfg = S3Config(bucket=BUCKET, region=region, access_key_id="k", secret_access_key="s")
    return S3ObjectStore(cfg)


def _body(admin: Any, key: str = KEY) -> bytes:
    return admin.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _etag(admin: Any, key: str = KEY) -> str:
    return admin.head_object(Bucket=BUCKET, Key=key)["ETag"]


def _kind(info: pytest.ExceptionInfo[ConnectorError]) -> str:
    err = info.value
    assert BUCKET not in str(err) and "zz_marker" not in str(err) and BUCKET not in repr(err)
    assert err.__cause__ is None and err.__context__ is None
    return err.kind


# AT-C03 -----------------------------------------------------------------------------------


SHAPES = pytest.mark.parametrize("size", [4, PART + 3], ids=["single_put", "multipart"])


@SHAPES
def test_put_new_returns_the_etag_of_the_object_it_made(admin: Any, size: int) -> None:
    data = b"m" * size
    version = _store().put_new(KEY, io.BytesIO(data))
    assert version and version == _etag(admin) and _body(admin) == data
    # A multipart ETag carries a part count; a single PUT's does not.
    assert ("-" in version) == (size > PART)


@pytest.mark.parametrize("size", [4, PART + 3], ids=["single_put", "multipart"])
def test_put_new_refuses_an_existing_object_and_leaves_it_alone(admin: Any, size: int) -> None:
    admin.put_object(Bucket=BUCKET, Key=KEY, Body=b"theirs")
    with pytest.raises(ConnectorError) as info:
        _store().put_new(KEY, io.BytesIO(b"x" * size))
    assert _kind(info) == "precondition_failed"
    assert _body(admin) == b"theirs"


# AT-C09a ----------------------------------------------------------------------------------


def test_two_create_only_writes_for_one_absent_key_have_one_winner(admin: Any) -> None:
    store = _store()
    store.put_new(KEY, io.BytesIO(b"first"))
    with pytest.raises(ConnectorError) as info:
        store.put_new(KEY, io.BytesIO(b"second"))
    assert _kind(info) == "precondition_failed"
    assert _body(admin) == b"first"


def test_put_if_without_a_version_is_create_only(admin: Any) -> None:
    store = _store()
    assert store.put_if(KEY, b"first", None) == "won"
    assert store.put_if(KEY, b"second", None) == "rejected"
    assert _body(admin) == b"first"


def test_put_if_compares_the_version(admin: Any) -> None:
    store = _store()
    store.put_if(KEY, b"one", None)
    stale = _etag(admin)
    assert store.put_if(KEY, b"two", stale) == "won"
    assert store.put_if(KEY, b"three", stale) == "rejected"
    assert _body(admin) == b"two"
    admin.delete_object(Bucket=BUCKET, Key=KEY)
    assert store.put_if(KEY, b"four", stale) == "rejected"


def test_put_if_maps_a_conflicting_write_and_an_unclassified_failure(
    admin: Any, wire: Wire
) -> None:
    store = _store()
    wire.answer = (409, "ConditionalRequestConflict")
    assert store.put_if(KEY, b"x", None) == "conflict"
    wire.answer = (500, "InternalError")
    assert store.put_if(KEY, b"x", None) == "unknown"
    wire.answer = (403, "AccessDenied")
    assert store.put_if(KEY, b"x", None) == "unknown"


# AT-C12b ----------------------------------------------------------------------------------


def test_a_lost_put_if_response_is_one_request_and_unknown(admin: Any, wire: Wire) -> None:
    store = _store()
    store.put_if(KEY, b"one", None)
    version = _etag(admin)
    wire.sent.clear()
    wire.lose_next_put = True
    assert store.put_if(KEY, b"two", version) == "unknown"
    assert wire.sent == ["PUT"]
    assert _body(admin) == b"two"


def test_a_failing_put_new_is_one_request(admin: Any, wire: Wire) -> None:
    wire.lose_next_put = True
    with pytest.raises(ConnectorError) as info:
        _store().put_new(KEY, io.BytesIO(b"mine"))
    assert _kind(info) == "transient"
    assert wire.sent == ["PUT"]


# AT-C22 -----------------------------------------------------------------------------------


def _eu_bucket(admin: Any) -> None:
    admin.create_bucket(
        Bucket="zz-eu-bkt", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"}
    )


def test_check_region_refuses_a_bucket_in_another_region(admin: Any) -> None:
    _eu_bucket(admin)
    cfg = S3Config(bucket="zz-eu-bkt", region="us-east-1", access_key_id="k", secret_access_key="s")
    with pytest.raises(ConnectorError) as info:
        S3ObjectStore(cfg).check_region()
    assert _kind(info) == "wrong_region"


def test_check_region_uses_the_region_the_sdk_resolves(
    admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _eu_bucket(admin)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    cfg = S3Config(bucket="zz-eu-bkt", access_key_id="k", secret_access_key="s")
    S3ObjectStore(cfg).check_region()


def test_check_region_accepts_the_matching_region(admin: Any) -> None:
    _store().check_region()


# AT-C29a ----------------------------------------------------------------------------------


def test_read_small_returns_bytes_and_version_within_the_limit(admin: Any) -> None:
    admin.put_object(Bucket=BUCKET, Key=KEY, Body=b"r" * 65)
    assert _store().read_small(KEY, 65) == (b"r" * 65, _etag(admin))
    assert _store().read_small("absent", 65) is None


def test_read_small_never_truncates_an_over_limit_object(admin: Any) -> None:
    admin.put_object(Bucket=BUCKET, Key=KEY, Body=b"20261112T000000-" + b"a" * 16 + b"\n" * 5000)
    with pytest.raises(ConnectorError) as info:
        _store().read_small(KEY, 65)
    assert _kind(info) == "pointer_invalid"
    admin.put_object(Bucket=BUCKET, Key=KEY, Body=b"r" * 66)
    with pytest.raises(ConnectorError) as info:
        _store().read_small(KEY, 65)
    assert _kind(info) == "pointer_invalid"


class _ShortReadBody:
    """A body that returns at most 32 bytes per read, as ``StreamingBody.read(n)`` may."""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return self._data.read(32 if n < 0 else min(n, 32))

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("size", "over"), [(65, False), (66, True), (500, True)])
def test_read_small_reads_through_short_reads(size: int, over: bool) -> None:
    body = _ShortReadBody(b"r" * size)

    class Client:
        def get_object(self, **kw: Any) -> dict[str, Any]:
            return {"Body": body, "ETag": '"e"'}

    store = _store()
    store._built[False] = Client()
    if over:
        with pytest.raises(ConnectorError) as info:
            store.read_small(KEY, 65)
        assert _kind(info) == "pointer_invalid"
    else:
        assert store.read_small(KEY, 65) == (b"r" * 65, '"e"')
    assert body.closed


# AT-C38 -----------------------------------------------------------------------------------


@SHAPES
def test_delete_created_removes_only_the_object_it_made(admin: Any, size: int) -> None:
    store = _store()
    version = store.put_new(KEY, io.BytesIO(b"m" * size))
    assert version == _etag(admin)
    store.delete_created(KEY, version)
    with pytest.raises(botocore_exc.ClientError):
        admin.head_object(Bucket=BUCKET, Key=KEY)


def test_delete_created_refuses_a_replaced_object(admin: Any) -> None:
    store = _store()
    version = store.put_new(KEY, io.BytesIO(b"mine"))
    admin.put_object(Bucket=BUCKET, Key=KEY, Body=b"theirs")
    with pytest.raises(ConnectorError) as info:
        store.delete_created(KEY, version)
    assert _kind(info) == "precondition_failed"
    assert _body(admin) == b"theirs"


# sources ----------------------------------------------------------------------------------


def test_iter_object_streams_the_object(admin: Any) -> None:
    admin.put_object(Bucket=BUCKET, Key=KEY, Body=b"a" * 10)
    assert b"".join(_store().iter_object(KEY)) == b"a" * 10


def test_gcs_iter_object_streams_through_the_gcs_open_path(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("google.cloud.storage")
    from decoy_engine.connectors import object_store
    from decoy_engine.connectors.gcs import GCSConfig

    class Blob:
        def open(self, mode: str, **kw: Any) -> io.BytesIO:
            assert mode == "rb" and kw["retry"] is not None
            return io.BytesIO(b"gcs-bytes")

    class Bucket:
        def blob(self, key: str) -> Blob:
            return Blob()

    class Client:
        def bucket(self, name: str) -> Bucket:
            return Bucket()

    monkeypatch.setattr(object_store, "_build_gcs_client", lambda config: Client())
    assert b"".join(GCSObjectStore(GCSConfig(bucket="b")).iter_object("k")) == b"gcs-bytes"

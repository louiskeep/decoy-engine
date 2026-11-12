"""S3 failure text and kind are fixed on every surface (AT-C10a, AT-C42, S3 half).

Stand-in SDK error objects carry an ARN, a bucket and a key in their message. None of it may
reach ``str()``, ``repr()``, ``args`` or the exception chain of anything the engine raises, and
each error carries a closed ``kind`` token that decides the retry class.
"""

from __future__ import annotations

import pickle
from collections.abc import Callable, Iterator
from typing import Any, get_args

import pytest

botocore_exc = pytest.importorskip("botocore.exceptions")
boto3 = pytest.importorskip("boto3")

from decoy_engine.connectors import _clients  # noqa: E402
from decoy_engine.connectors.s3 import S3Config, S3FileSink, S3FileSource  # noqa: E402
from decoy_engine.errors import ConnectorErrorKind  # noqa: E402
from decoy_engine.sdk import ConnectorError, PermanentError, TransientError  # noqa: E402

ARN = "arn:aws:iam::123456789012:user/zz_marker_user"
MSG = f"{ARN} cannot read zz_marker_bkt/zz_marker_key at https://zz-marker-host/zz_marker_bkt"
MARKERS = ("zz_marker", "zz-marker-host", "123456789012")


def client_error(code: str, status: int | None = None) -> Exception:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": MSG}}
    if status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": status}
    return botocore_exc.ClientError(response, "GetObject")


def _net(name: str) -> Callable[[], Exception]:
    return lambda: getattr(botocore_exc, name)(endpoint_url="https://zz-marker-host/zz_marker_bkt")


# (stand-in error factory, expected kind)
KINDS: dict[str, tuple[Callable[[], BaseException], str]] = {
    "NoSuchBucket": (lambda: client_error("NoSuchBucket"), "not_found"),
    "NoSuchKey": (lambda: client_error("NoSuchKey"), "not_found"),
    "404": (lambda: client_error("404"), "not_found"),
    "NotFound": (lambda: client_error("NotFound"), "not_found"),
    "403": (lambda: client_error("403"), "access_denied"),
    "AccessDenied": (lambda: client_error("AccessDenied", 403), "access_denied"),
    "AllAccessDisabled": (lambda: client_error("AllAccessDisabled"), "access_denied"),
    "InvalidAccessKeyId": (lambda: client_error("InvalidAccessKeyId", 403), "credentials"),
    "SignatureDoesNotMatch": (lambda: client_error("SignatureDoesNotMatch", 403), "credentials"),
    "ExpiredToken": (lambda: client_error("ExpiredToken", 400), "credentials"),
    "NoCredentialsError": (lambda: botocore_exc.NoCredentialsError(), "credentials"),
    "PreconditionFailed": (lambda: client_error("PreconditionFailed", 412), "precondition_failed"),
    "412": (lambda: client_error("412"), "precondition_failed"),
    "ConditionalRequestConflict": (
        lambda: client_error("ConditionalRequestConflict", 409),
        "conflict",
    ),
    "409": (lambda: client_error("409"), "conflict"),
    "PermanentRedirect": (lambda: client_error("PermanentRedirect", 301), "wrong_region"),
    "301": (lambda: client_error("301"), "wrong_region"),
    "AuthorizationHeaderMalformed": (
        lambda: client_error("AuthorizationHeaderMalformed", 400),
        "wrong_region",
    ),
    "SlowDown": (lambda: client_error("SlowDown", 503), "transient"),
    "InternalError": (lambda: client_error("InternalError", 500), "transient"),
    "ServiceUnavailable": (lambda: client_error("ServiceUnavailable", 503), "transient"),
    "RequestTimeout": (lambda: client_error("RequestTimeout", 400), "transient"),
    "unlisted-5xx": (lambda: client_error("Whatever", 502), "transient"),
    "EndpointConnectionError": (_net("EndpointConnectionError"), "transient"),
    "ConnectTimeoutError": (_net("ConnectTimeoutError"), "transient"),
    "ReadTimeoutError": (_net("ReadTimeoutError"), "transient"),
    "ConnectionClosedError": (_net("ConnectionClosedError"), "transient"),
    "unlisted-4xx": (lambda: client_error("EntityTooLarge", 400), "rejected"),
    "no-status": (lambda: client_error("Whatever"), "unexpected"),
    "RuntimeError": (lambda: RuntimeError(MSG), "unexpected"),
}
CASES = sorted(KINDS)


def _value_free(err: BaseException) -> None:
    seen = [str(err), repr(err), *map(str, err.args), *map(str, vars(err).values())]
    for text in seen:
        for marker in MARKERS:
            assert marker not in text, f"error carries {marker!r}: {text!r}"


def _unchained(err: BaseException) -> None:
    assert err.__cause__ is None
    assert err.__context__ is None


@pytest.mark.parametrize("case", CASES)
def test_s3_error_kind_class_and_fixed_text(case: str) -> None:
    make, kind = KINDS[case]
    err = _clients.s3_error(make())
    assert err.kind == kind  # type: ignore[attr-defined]
    assert kind in get_args(ConnectorErrorKind)
    assert type(err) is (TransientError if kind == "transient" else PermanentError)
    _value_free(err)
    _unchained(err)


@pytest.mark.parametrize(
    "code", ["PreconditionFailed", "ConditionalRequestConflict", "PermanentRedirect"]
)
def test_conditional_and_redirect_codes_are_not_retried(code: str) -> None:
    """These three fell into the transient bucket before the kind table."""
    err = _clients.s3_error(client_error(code))
    assert type(err) is PermanentError


def test_every_kind_the_tables_can_build_is_in_the_closed_set() -> None:
    kinds = {kind for _make, kind in KINDS.values()}
    assert kinds <= set(get_args(ConnectorErrorKind))
    assert set(get_args(ConnectorErrorKind)) - kinds == {"pointer_invalid"}


@pytest.mark.parametrize("case", ["AccessDenied", "SlowDown", "ConnectTimeoutError"])
def test_kind_survives_a_pickle_round_trip(case: str) -> None:
    make, kind = KINDS[case]
    err = _clients.s3_error(make())
    again = pickle.loads(pickle.dumps(err))  # noqa: S301
    assert type(again) is type(err)
    assert again.kind == kind
    assert str(again) == str(err)


def test_a_classified_error_passes_through_unchanged() -> None:
    first = _clients.s3_error(client_error("AccessDenied"))
    assert _clients.s3_error(first) is first


def test_connector_error_kind_defaults_to_unexpected() -> None:
    assert ConnectorError("x").kind == "unexpected"  # type: ignore[attr-defined]
    assert PermanentError("x", kind="conflict").kind == "conflict"  # type: ignore[call-arg]


# ----- AT-C10a: raise sites ---------------------------------------------------------


class _Pager:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def paginate(self, **_kwargs: Any) -> Iterator[Any]:
        raise self._exc
        yield  # pragma: no cover


class FakeS3:
    """Fails one named client method; every other call succeeds."""

    def __init__(self, fail: dict[str, BaseException]) -> None:
        self.fail = fail

    def _hit(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def head_bucket(self, **_kw: Any) -> dict:
        self._hit("head_bucket")
        return {}

    def head_object(self, **_kw: Any) -> dict:
        self._hit("head_object")
        return {}

    def get_object(self, **_kw: Any) -> dict:
        self._hit("get_object")
        return {"Body": type("B", (), {"iter_chunks": lambda *a, **k: iter([b"x"]), "close": int})}

    def get_paginator(self, _name: str) -> _Pager:
        return _Pager(self.fail["list"])

    def put_object(self, **_kw: Any) -> dict:
        self._hit("put_object")
        return {}

    def create_multipart_upload(self, **_kw: Any) -> dict:
        self._hit("create_multipart_upload")
        return {"UploadId": "u"}

    def upload_part(self, **_kw: Any) -> dict:
        self._hit("upload_part")
        return {"ETag": "e"}

    def complete_multipart_upload(self, **_kw: Any) -> dict:
        self._hit("complete_multipart_upload")
        return {}

    def abort_multipart_upload(self, **_kw: Any) -> dict:
        return {}


CFG = S3Config(bucket="zz_marker_bkt", region="us-east-1")
BIG = [b"x" * (5 * 1024 * 1024 + 1)]
POINTS: dict[str, tuple[str, Callable[[], Any]]] = {
    "head": ("head_object", lambda: S3FileSource(CFG).head("zz_marker_key")),
    "open": ("get_object", lambda: list(S3FileSource(CFG).open("zz_marker_key"))),
    "list": ("list", lambda: list(S3FileSource(CFG).list())),
    "put": ("put_object", lambda: S3FileSink(CFG).write("zz_marker_key", iter([b"x"]))),
    "initiate": ("create_multipart_upload", lambda: S3FileSink(CFG).write("k", iter(BIG))),
    "part": ("upload_part", lambda: S3FileSink(CFG).write("k", iter(BIG))),
    "complete": ("complete_multipart_upload", lambda: S3FileSink(CFG).write("k", iter(BIG))),
}


@pytest.mark.parametrize("point", sorted(POINTS))
@pytest.mark.parametrize("case", ["AccessDenied", "SlowDown", "EndpointConnectionError", "404"])
def test_raised_s3_errors_are_fixed_text_and_unchained(
    case: str, point: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    make, kind = KINDS[case]
    where, run = POINTS[point]
    fake = FakeS3({where: make()})
    monkeypatch.setattr(_clients, "s3_client", lambda **_kw: fake)
    with pytest.raises(ConnectorError) as info:
        run()
    err = info.value
    assert err.kind == kind  # type: ignore[attr-defined]
    _value_free(err)
    _unchained(err)


@pytest.mark.parametrize("cls", [S3FileSource, S3FileSink])
def test_check_detail_is_value_free(cls: type, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3({"head_bucket": client_error("AccessDenied", 403)})
    monkeypatch.setattr(_clients, "s3_client", lambda **_kw: fake)
    result = cls(CFG).check()
    assert result.ok is False
    for marker in MARKERS:
        assert marker not in (result.detail or "")


def test_an_already_classified_error_is_raised_as_is() -> None:
    """A raise site whose ``exc`` was classified by an inner guard must not chain to itself."""
    inner = PermanentError("fixed")

    def call() -> None:
        raise inner

    with pytest.raises(PermanentError) as info:
        _clients.s3_call(call)
    assert info.value is inner
    _unchained(info.value)


# ----- the one client factory -------------------------------------------------------


def _built(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_client(service: str, **kw: Any) -> object:
        seen["service"] = service
        seen.update(kw)
        return object()

    monkeypatch.setattr(boto3, "client", fake_client)
    base: dict[str, Any] = {
        "region": None,
        "endpoint_url": None,
        "access_key_id": None,
        "secret_access_key": None,
    }
    _clients.s3_client(**{**base, **kwargs})
    return seen


def test_client_sends_one_request_with_no_sdk_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _built(monkeypatch)
    cfg = seen["config"]
    assert seen["service"] == "s3"
    assert cfg.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert (cfg.connect_timeout, cfg.read_timeout) == (5, 60)


def test_client_omits_region_credentials_and_endpoint_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _built(monkeypatch)
    for key in ("region_name", "aws_access_key_id", "aws_secret_access_key"):
        assert key not in seen
    assert seen.get("endpoint_url") is None


def test_client_forwards_what_the_caller_set(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _built(
        monkeypatch,
        region="eu-west-2",
        endpoint_url="http://localhost:9000",
        access_key_id="id",
        secret_access_key="sk",
    )
    assert seen["region_name"] == "eu-west-2"
    assert seen["endpoint_url"] == "http://localhost:9000"
    assert (seen["aws_access_key_id"], seen["aws_secret_access_key"]) == ("id", "sk")


def test_s3_config_region_is_optional() -> None:
    assert S3Config(bucket="b").region is None

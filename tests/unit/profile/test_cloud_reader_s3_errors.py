"""Profiler S3 failures are classified connector errors with fixed text (AT-C41)."""

from __future__ import annotations

from typing import Any

import pytest

botocore_exc = pytest.importorskip("botocore.exceptions")

from decoy_engine.connectors import _clients  # noqa: E402
from decoy_engine.profile._cloud_readers import S3CsvSource  # noqa: E402
from decoy_engine.sdk import PermanentError, TransientError  # noqa: E402

MSG = "arn:aws:iam::123456789012:user/zz_marker_user zz_marker_bkt/zz_marker_key"


class _Failing:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def head_object(self, **_kw: Any) -> dict:
        raise self._exc


def _source(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> S3CsvSource:
    monkeypatch.setattr(_clients, "s3_client", lambda **_kw: _Failing(exc))
    return S3CsvSource({"bucket": "zz_marker_bkt", "key": "zz_marker_key", "format": "csv"})


@pytest.mark.parametrize(
    ("exc", "cls"),
    [
        (
            botocore_exc.ClientError({"Error": {"Code": "AccessDenied", "Message": MSG}}, "Head"),
            PermanentError,
        ),
        (
            botocore_exc.EndpointConnectionError(endpoint_url="https://zz-marker-host"),
            TransientError,
        ),
    ],
)
def test_profiler_s3_errors_are_fixed_text_and_unchained(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, cls: type
) -> None:
    with pytest.raises(cls) as info:
        _source(monkeypatch, exc).row_count()
    err = info.value
    assert type(err) is cls
    for marker in ("zz_marker", "zz-marker-host", "123456789012"):
        assert marker not in str(err) + repr(err)
    assert err.__cause__ is None and err.__context__ is None


class _ReadFailing:
    """A client whose object body fails on read, like a connection dropped mid-transfer."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def head_object(self, **_kw: Any) -> dict:
        return {"ContentLength": 10}

    def get_object(self, **_kw: Any) -> dict:
        exc = self._exc

        class Body:
            def __enter__(self) -> Body:
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

            def read(self, *_a: Any) -> bytes:
                raise exc

        return {"Body": Body()}


_STREAM = [
    (botocore_exc.ReadTimeoutError(endpoint_url="https://zz-marker-host"), "transient"),
    (botocore_exc.ResponseStreamingError(error="zz_marker broke"), "transient"),
    (botocore_exc.IncompleteReadError(actual_bytes=1, expected_bytes=2), "unexpected"),
]


@pytest.mark.parametrize(("exc", "kind"), _STREAM, ids=lambda v: type(v).__name__)
@pytest.mark.parametrize("read", ["range", "full"])
def test_profiler_body_read_failures_are_classified_and_unchained(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, kind: str, read: str
) -> None:
    monkeypatch.setattr(_clients, "s3_client", lambda **_kw: _ReadFailing(exc))
    source = S3CsvSource({"bucket": "zz_marker_bkt", "key": "zz_marker_key", "format": "csv"})
    with pytest.raises((PermanentError, TransientError)) as info:
        source.row_count() if read == "range" else source.to_frame()
    err = info.value
    assert err.kind == kind  # type: ignore[attr-defined]
    for marker in ("zz_marker", "zz-marker-host"):
        assert marker not in str(err) + repr(err)
    assert err.__cause__ is None and err.__context__ is None

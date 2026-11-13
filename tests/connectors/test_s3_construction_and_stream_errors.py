"""S3 client construction and body-stream failures leave as classified errors.

Construction (bad endpoint, half-set credentials, unknown profile) and mid-stream reads
(timeout, truncated body) are SDK calls like any other: fixed text, a closed ``kind`` and no
exception chain.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

botocore_exc = pytest.importorskip("botocore.exceptions")
pytest.importorskip("boto3")

from decoy_engine.connectors import _clients  # noqa: E402
from decoy_engine.connectors.s3 import S3Config, S3FileSink, S3FileSource  # noqa: E402
from decoy_engine.sdk import ConnectorError  # noqa: E402

MARKERS = ("zz_marker", "zz-marker", "zz marker")


def _clean(err: BaseException) -> None:
    text = str(err) + repr(err) + repr(err.args)
    for marker in MARKERS:
        assert marker not in text
    assert err.__cause__ is None and err.__context__ is None


def _build(**kw: Any) -> Any:
    args: dict[str, Any] = {
        "region": None,
        "endpoint_url": None,
        "access_key_id": None,
        "secret_access_key": None,
        "single_request": False,
    }
    return _clients.s3_client(**{**args, **kw})


# ----- construction ------------------------------------------------------------------


def _bad_endpoint(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return {"endpoint_url": "zz marker not a url"}


def _half_credentials(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return {"access_key_id": "zz_marker_key"}


def _unknown_profile(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("AWS_PROFILE", "zz_marker_profile")
    return {}


CONSTRUCTION = {
    "invalid endpoint": (_bad_endpoint, "unexpected"),
    "partial credentials": (_half_credentials, "credentials"),
    "unknown profile": (_unknown_profile, "unexpected"),
}


@pytest.mark.parametrize("family", sorted(CONSTRUCTION))
def test_construction_errors_are_classified_and_unchained(
    family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, kind = CONSTRUCTION[family]
    kwargs = setup(monkeypatch)
    with pytest.raises(ConnectorError) as info:
        _build(**kwargs)
    assert info.value.kind == kind  # type: ignore[attr-defined]
    _clean(info.value)


@pytest.mark.parametrize("cls", [S3FileSource, S3FileSink])
@pytest.mark.parametrize("family", sorted(CONSTRUCTION))
def test_check_reports_a_construction_error_instead_of_raising(
    cls: type, family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, _ = CONSTRUCTION[family]
    kwargs = setup(monkeypatch)
    cfg = S3Config(
        bucket="bkt",
        endpoint_url=kwargs.get("endpoint_url"),
        access_key_id=kwargs.get("access_key_id"),
    )
    result = cls(cfg).check()
    assert result.ok is False
    for marker in MARKERS:
        assert marker not in (result.detail or "")


# ----- mid-stream reads --------------------------------------------------------------

STREAM_ERRORS = {
    "ReadTimeoutError": (
        lambda: botocore_exc.ReadTimeoutError(endpoint_url="https://zz-marker-host/zz_marker"),
        "transient",
    ),
    "ResponseStreamingError": (
        lambda: botocore_exc.ResponseStreamingError(error="zz_marker broke"),
        "transient",
    ),
    "IncompleteReadError": (
        lambda: botocore_exc.IncompleteReadError(actual_bytes=1, expected_bytes=2),
        "unexpected",
    ),
}


class Body:
    """A response body that yields one chunk, then fails like a dropped connection."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.closed = False

    def iter_chunks(self, chunk_size: int = 0) -> Iterator[bytes]:
        yield b"first"
        raise self.exc

    def read(self, *_a: Any) -> bytes:
        raise self.exc

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Body:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class Client:
    def __init__(self, body: Body) -> None:
        self.body = body

    def get_object(self, **_kw: Any) -> dict:
        return {"Body": self.body}


@pytest.mark.parametrize("name", sorted(STREAM_ERRORS))
def test_open_translates_a_mid_stream_failure(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    make, kind = STREAM_ERRORS[name]
    body = Body(make())
    monkeypatch.setattr(_clients, "s3_client", lambda **_kw: Client(body))
    stream = S3FileSource(S3Config(bucket="bkt", region="us-east-1")).open("k")
    assert next(stream) == b"first"
    with pytest.raises(ConnectorError) as info:
        next(stream)
    assert info.value.kind == kind  # type: ignore[attr-defined]
    _clean(info.value)
    assert body.closed

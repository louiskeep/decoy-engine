"""The one S3 put path: single PUT, multipart, conditions and abort (risk row 9).

A stand-in client records every call, so the part size, the call order and the conditions
each call carries are pinned without moto.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

botocore_exc = pytest.importorskip("botocore.exceptions")

from decoy_engine.connectors import _clients  # noqa: E402
from decoy_engine.connectors.s3 import S3Config, S3FileSink  # noqa: E402
from decoy_engine.sdk import PermanentError, TransientError  # noqa: E402

PART = 5 * 1024 * 1024
MARKER = "zz_marker_bkt zz_marker_key"


class Recorder:
    """Records each call's name and keyword arguments; one named method can fail."""

    def __init__(self, fail: dict[str, BaseException] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail or {}

    def _do(self, name: str, kw: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, kw))
        if name in self.fail:
            raise self.fail[name]
        return result

    def put_object(self, **kw: Any) -> dict:
        return self._do("put_object", kw, {})

    def create_multipart_upload(self, **kw: Any) -> dict:
        return self._do("create_multipart_upload", kw, {"UploadId": "u1"})

    def upload_part(self, **kw: Any) -> dict:
        return self._do("upload_part", kw, {"ETag": f"e{kw['PartNumber']}"})

    def complete_multipart_upload(self, **kw: Any) -> dict:
        return self._do("complete_multipart_upload", kw, {})

    def abort_multipart_upload(self, **kw: Any) -> dict:
        return self._do("abort_multipart_upload", kw, {})

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _put(client: Recorder, chunks: Iterator[bytes], **conditions: str) -> int:
    return _clients.s3_put(client, "bkt", "k", chunks, conditions)


def test_small_body_is_one_put_with_the_conditions() -> None:
    client = Recorder()
    assert _put(client, iter([b"ab", b"", b"cd"]), IfNoneMatch="*") == 4
    assert client.calls == [
        ("put_object", {"Bucket": "bkt", "Key": "k", "Body": b"abcd", "IfNoneMatch": "*"})
    ]


def test_multipart_uses_exact_part_sizes_and_conditions_only_on_complete() -> None:
    client = Recorder()
    total = _put(client, iter([b"x" * PART, b"y" * (PART + 7), b"z" * 3]), IfNoneMatch="*")
    assert total == 2 * PART + 10
    assert client.names() == [
        "create_multipart_upload",
        "upload_part",
        "upload_part",
        "upload_part",
        "complete_multipart_upload",
    ]
    sizes = [len(kw["Body"]) for name, kw in client.calls if name == "upload_part"]
    assert sizes == [PART, PART, 10]
    for name, kw in client.calls[:-1]:
        assert "IfNoneMatch" not in kw, name
    name, kw = client.calls[-1]
    assert kw["IfNoneMatch"] == "*"
    assert kw["UploadId"] == "u1"
    assert kw["MultipartUpload"] == {
        "Parts": [{"PartNumber": n, "ETag": f"e{n}"} for n in (1, 2, 3)]
    }


def test_a_body_of_exact_parts_has_no_tail_part() -> None:
    client = Recorder()
    assert _put(client, iter([b"x" * (2 * PART)])) == 2 * PART
    assert client.names().count("upload_part") == 2
    assert client.names()[-1] == "complete_multipart_upload"


def test_a_failed_part_aborts_once_and_leaves_value_free_and_unchained() -> None:
    exc = botocore_exc.ClientError(
        {"Error": {"Code": "AccessDenied", "Message": MARKER}, "ResponseMetadata": {}}, "Op"
    )
    client = Recorder(fail={"upload_part": exc})
    with pytest.raises(PermanentError) as info:
        _put(client, iter([b"x" * (PART + 1)]))
    assert client.names() == ["create_multipart_upload", "upload_part", "abort_multipart_upload"]
    assert client.calls[-1][1] == {"Bucket": "bkt", "Key": "k", "UploadId": "u1"}
    assert "zz_marker" not in str(info.value) + repr(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_a_failing_source_aborts_and_the_original_error_propagates() -> None:
    class BoomError(Exception):
        pass

    def chunks() -> Iterator[bytes]:
        yield b"x" * PART
        raise BoomError

    client = Recorder()
    with pytest.raises(BoomError):
        _put(client, chunks())
    assert client.names() == ["create_multipart_upload", "upload_part", "abort_multipart_upload"]


def test_a_failing_abort_does_not_mask_the_write_failure() -> None:
    first = botocore_exc.ClientError({"Error": {"Code": "SlowDown"}}, "Op")
    client = Recorder(fail={"upload_part": first, "abort_multipart_upload": RuntimeError("x")})
    with pytest.raises(TransientError):
        _put(client, iter([b"x" * PART]))


def test_the_sink_writes_through_the_one_put_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def spy(client: Any, bucket: str, key: str, chunks: Iterator[bytes], conditions: Any) -> int:
        seen.update(client=client, bucket=bucket, key=key, conditions=conditions)
        seen["body"] = b"".join(chunks)
        return 3

    stand_in = object()
    monkeypatch.setattr(_clients, "s3_client", lambda **_kw: stand_in)
    monkeypatch.setattr(_clients, "s3_put", spy)
    result = S3FileSink(S3Config(bucket="bkt", prefix="p/")).write("a.csv", iter([b"a", b"bc"]))
    assert seen == {
        "client": stand_in,
        "bucket": "bkt",
        "key": "p/a.csv",
        "conditions": {},
        "body": b"abc",
    }
    assert (result.path, result.bytes_written) == ("p/a.csv", 3)

"""moto honours S3 conditional requests, and the SDK floor carries them (AT-C31, AT-C33a)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

boto3 = pytest.importorskip("boto3")
botocore_exc = pytest.importorskip("botocore.exceptions")
mock_aws = pytest.importorskip("moto").mock_aws

from botocore.session import get_session  # noqa: E402

from decoy_engine.connectors import _clients  # noqa: E402

BUCKET = "test-bucket"
KEY = "obj"


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = _clients.s3_client(
            region="us-east-1",
            endpoint_url=None,
            access_key_id="k",
            secret_access_key="s",
            single_request=True,
        )
        client.create_bucket(Bucket=BUCKET)
        yield client


def _stale_etag(s3: Any) -> str:
    stale = s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"one")["ETag"]
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"two")
    return stale


def _refused(call: Any, **kw: Any) -> None:
    with pytest.raises(botocore_exc.ClientError) as info:
        call(**kw)
    assert info.value.response["ResponseMetadata"]["HTTPStatusCode"] == 412


def test_put_if_none_match_refuses_an_existing_key(s3: Any) -> None:
    _stale_etag(s3)
    _refused(s3.put_object, Bucket=BUCKET, Key=KEY, Body=b"x", IfNoneMatch="*")


def test_put_if_match_refuses_a_stale_etag(s3: Any) -> None:
    stale = _stale_etag(s3)
    _refused(s3.put_object, Bucket=BUCKET, Key=KEY, Body=b"x", IfMatch=stale)


def test_complete_multipart_if_none_match_refuses_an_existing_key(s3: Any) -> None:
    _stale_etag(s3)
    upload = s3.create_multipart_upload(Bucket=BUCKET, Key=KEY)["UploadId"]
    etag = s3.upload_part(Bucket=BUCKET, Key=KEY, UploadId=upload, PartNumber=1, Body=b"x")["ETag"]
    _refused(
        s3.complete_multipart_upload,
        Bucket=BUCKET,
        Key=KEY,
        UploadId=upload,
        MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": etag}]},
        IfNoneMatch="*",
    )


def test_delete_if_match_refuses_a_stale_etag_and_keeps_the_object(s3: Any) -> None:
    stale = _stale_etag(s3)
    _refused(s3.delete_object, Bucket=BUCKET, Key=KEY, IfMatch=stale)
    assert s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read() == b"two"


def test_installed_sdk_model_carries_the_conditional_parameters() -> None:
    model = get_session().get_service_model("s3")
    wanted = {
        "PutObject": {"IfNoneMatch", "IfMatch"},
        "CompleteMultipartUpload": {"IfNoneMatch", "IfMatch"},
        "DeleteObject": {"IfMatch"},
    }
    for operation, params in wanted.items():
        members = set(model.operation_model(operation).input_shape.members)
        assert params <= members, operation

"""S3FileSource + S3FileSink contract tests.

Uses moto's `mock_aws` fixture so the tests never touch real AWS. The
S3FileSourceContract / S3FileSinkContract subclasses provide the
fixtures that the shared `FileSourceContract` / `FileSinkContract` base
classes (from sdk_contract_tests) need.
"""
from __future__ import annotations

import pytest

# Skip the whole module when optional cloud-storage deps aren't installed.
# `pip install -e .[dev]` brings boto3 + moto in transitively; bare pytest
# runs on a slim install would otherwise fail at collection time.
boto3 = pytest.importorskip("boto3")
mock_aws = pytest.importorskip("moto").mock_aws

from decoy_engine.connectors.s3 import S3Config, S3FileSink, S3FileSource  # noqa: E402
from decoy_engine.sdk import PermanentError  # noqa: E402

from sdk_contract_tests import FileSinkContract, FileSourceContract  # noqa: E402

BUCKET = "test-bucket"
REGION = "us-east-1"


# ----- shared moto + bucket setup ----------------------------------------


@pytest.fixture
def aws_mocked():
    with mock_aws():
        yield


@pytest.fixture
def boto_client(aws_mocked):
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture
def s3_config() -> S3Config:
    # Moto accepts any credentials when mock_aws is active.
    return S3Config(
        bucket=BUCKET,
        region=REGION,
        access_key_id="test-key",
        secret_access_key="test-secret",
    )


# ----- contract conformance (FileSource side) -----------------------------


class TestS3FileSourceContract(FileSourceContract):
    """Runs every FileSourceContract test against S3FileSource."""

    @pytest.fixture
    def source(self, s3_config, boto_client):
        # Seed one fixture file so list() and open() have something to chew on.
        boto_client.put_object(
            Bucket=BUCKET,
            Key="fixture/hello.txt",
            Body=b"contract-fixture-body\n",
            ContentType="text/plain",
        )
        return S3FileSource(s3_config)

    @pytest.fixture
    def seeded_path(self):
        # Path the `source` fixture seeded; contract tests use this for
        # open() + introspection verification.
        return "fixture/hello.txt"


# ----- contract conformance (FileSink side) -------------------------------


class TestS3FileSinkContract(FileSinkContract):
    """Runs every FileSinkContract test against S3FileSink."""

    @pytest.fixture
    def sink(self, s3_config, boto_client):
        return S3FileSink(s3_config)

    @pytest.fixture
    def reader_for(self, boto_client):
        """Return a callable that reads back what the sink just wrote.

        The contract suite uses this for `test_write_round_trip` and
        `test_multipart_round_trip_when_advertised`. Same boto client
        the sink wrote against; same moto state.
        """

        def _read(path: str) -> bytes:
            return boto_client.get_object(Bucket=BUCKET, Key=path)["Body"].read()

        return _read


# ----- S3-specific behavior tests beyond the generic contract ------------


class TestS3SourceBehavior:
    """Things specific to S3 / boto3 / moto that the generic contract
    doesn't cover but matter for real-world correctness."""

    def test_list_returns_size_for_seeded_object(self, s3_config, boto_client):
        body = b"size-check-body\n"
        boto_client.put_object(Bucket=BUCKET, Key="sized.bin", Body=body)
        source = S3FileSource(s3_config)
        meta = next(m for m in source.list() if m.path == "sized.bin")
        assert meta.size == len(body)
        assert meta.modified is not None

    def test_list_with_call_prefix_filters(self, s3_config, boto_client):
        for path in ("a/one.txt", "a/two.txt", "b/three.txt"):
            boto_client.put_object(Bucket=BUCKET, Key=path, Body=b"x")
        source = S3FileSource(s3_config)
        a_listing = sorted(m.path for m in source.list(prefix="a/"))
        assert a_listing == ["a/one.txt", "a/two.txt"]

    def test_head_returns_content_type(self, s3_config, boto_client):
        # S3FileSource overrides the default head() with a native head_object
        # call that returns ContentType (which list() can't).
        boto_client.put_object(
            Bucket=BUCKET,
            Key="typed.json",
            Body=b'{"a":1}',
            ContentType="application/json",
        )
        source = S3FileSource(s3_config)
        meta = source.head("typed.json")
        assert meta.size == len(b'{"a":1}')
        assert meta.content_type == "application/json"

    def test_head_missing_key_raises_permanent(self, s3_config, boto_client):
        source = S3FileSource(s3_config)
        with pytest.raises(PermanentError):
            source.head("never-existed-xyz.bin")

    def test_open_streams_full_body(self, s3_config, boto_client):
        body = b"streaming-body " * 100  # 1500 bytes; small but multi-iter-friendly
        boto_client.put_object(Bucket=BUCKET, Key="streamed.bin", Body=body)
        source = S3FileSource(s3_config)
        chunks = list(source.open("streamed.bin"))
        assert b"".join(chunks) == body

    def test_open_missing_key_raises_permanent(self, s3_config, boto_client):
        source = S3FileSource(s3_config)
        with pytest.raises(PermanentError):
            list(source.open("never-existed.bin"))

    def test_check_missing_bucket_returns_not_ok(self, aws_mocked):
        # No bucket created in this test; head_bucket should fail.
        cfg = S3Config(
            bucket="absent-bucket",
            region=REGION,
            access_key_id="test",
            secret_access_key="test",
        )
        result = S3FileSource(cfg).check()
        assert result.ok is False
        assert result.detail

    def test_check_existing_bucket_returns_ok(self, s3_config, boto_client):
        result = S3FileSource(s3_config).check()
        assert result.ok is True

    def test_config_prefix_scopes_listing(self, aws_mocked):
        # A config prefix scopes the source so list() never sees outside it.
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        client.put_object(Bucket=BUCKET, Key="scoped/inside.txt", Body=b"yes")
        client.put_object(Bucket=BUCKET, Key="outside.txt", Body=b"no")
        cfg = S3Config(
            bucket=BUCKET,
            prefix="scoped",
            region=REGION,
            access_key_id="test",
            secret_access_key="test",
        )
        listing = [m.path for m in S3FileSource(cfg).list()]
        assert "scoped/inside.txt" in listing
        assert "outside.txt" not in listing


class TestS3SinkBehavior:
    """Sink-side specifics: small-file single PUT, large-file multipart,
    abort-on-error, prefix joining."""

    def test_small_file_single_put(self, s3_config, boto_client):
        body = b"hello small file\n"
        sink = S3FileSink(s3_config)
        result = sink.write("small.txt", iter([body]))
        assert result.bytes_written == len(body)
        assert boto_client.get_object(Bucket=BUCKET, Key="small.txt")["Body"].read() == body

    def test_multi_chunk_under_threshold_concatenated(self, s3_config, boto_client):
        # Three sub-5MB chunks: combined into one buffer, single PUT.
        chunks = [b"alpha-", b"beta-", b"gamma"]
        sink = S3FileSink(s3_config)
        sink.write("concat.txt", iter(chunks))
        got = boto_client.get_object(Bucket=BUCKET, Key="concat.txt")["Body"].read()
        assert got == b"".join(chunks)

    def test_large_file_uses_multipart_and_roundtrips(self, s3_config, boto_client):
        # Build a 12 MiB body so the sink crosses the 5 MiB multipart threshold
        # multiple times. Verify the reassembled body matches byte-for-byte.
        chunks = [b"x" * (1 * 1024 * 1024) for _ in range(12)]
        sink = S3FileSink(s3_config)
        result = sink.write("large.bin", iter(chunks))
        assert result.bytes_written == 12 * 1024 * 1024
        got = boto_client.get_object(Bucket=BUCKET, Key="large.bin")["Body"].read()
        assert got == b"".join(chunks)

    def test_empty_input_writes_empty_object(self, s3_config, boto_client):
        # An iterator that yields nothing should still produce an
        # empty object via single PUT (zero-byte put_object is legal).
        sink = S3FileSink(s3_config)
        result = sink.write("empty.bin", iter([]))
        assert result.bytes_written == 0
        assert boto_client.get_object(Bucket=BUCKET, Key="empty.bin")["Body"].read() == b""

    def test_prefix_in_config_joined_to_write_path(self, aws_mocked):
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        cfg = S3Config(
            bucket=BUCKET,
            prefix="outputs",
            region=REGION,
            access_key_id="test",
            secret_access_key="test",
        )
        result = S3FileSink(cfg).write("subdir/file.txt", iter([b"prefixed"]))
        assert result.path == "outputs/subdir/file.txt"
        got = client.get_object(Bucket=BUCKET, Key="outputs/subdir/file.txt")["Body"].read()
        assert got == b"prefixed"

    def test_write_to_missing_bucket_raises_permanent(self, aws_mocked):
        cfg = S3Config(
            bucket="never-created",
            region=REGION,
            access_key_id="test",
            secret_access_key="test",
        )
        with pytest.raises(PermanentError):
            S3FileSink(cfg).write("any.txt", iter([b"body"]))

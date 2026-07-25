"""GCSFileSource + GCSFileSink contract tests.

google-cloud-storage has no widely-used in-process mock akin to moto for
S3. This module installs a fake `Client` / `Bucket` / `Blob` on the
storage module surface for the duration of each test. The fake operates
on a single dict[str, dict] that represents the remote bucket; reads
and writes mutate the same dict so tests can pre-seed and assert.

What the fake covers: `Client.bucket`, `Client.get_bucket`,
`Bucket.list_blobs`, `Bucket.blob`, `Blob.reload`, `Blob.open` (read
and write modes), plus the metadata fields `name`, `size`,
`content_type`, `updated`.

What the fake does not cover: signed-URL generation, resumable-session
state, server-side encryption. Tests that need those should run against
the real GCS or gcp-storage-emulator (out of scope for Sprint G Week 3).
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

# Skip the whole module when google-cloud-storage isn't installed. It's
# an opt-in extra (`pip install -e .[gcs]`) because it pulls grpcio +
# the rest of the GCP client stack; S3-only installs don't need it.
# Submodule import: pytest.importorskip on the parent + attribute access
# doesn't trigger submodule loading in all environments, so import the
# submodule directly. Skips the file cleanly when the cloud SDK is absent.
gax_exc = pytest.importorskip("google.api_core.exceptions")

from sdk_contract_tests import FileSinkContract, FileSourceContract  # noqa: E402

from decoy_engine.connectors.gcs import (  # noqa: E402
    GCSConfig,
    GCSFileSink,
    GCSFileSource,
)
from decoy_engine.sdk import PermanentError  # noqa: E402

# ----- Fake google-cloud-storage layer -----------------------------------


class _FakeBlobFile:
    """File-like wrapper used by both read and write Blob.open() modes."""

    def __init__(self, store: dict, key: str, mode: str):
        self.store = store
        self.key = key
        self.mode = mode
        if "r" in mode:
            if key not in store:
                raise gax_exc.NotFound(f"No such object: {key}")
            self._buf = io.BytesIO(store[key]["body"])
        else:
            self._buf = io.BytesIO()

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def write(self, data: bytes) -> int:
        return self._buf.write(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if "w" in self.mode:
            now = datetime.now(timezone.utc)
            self.store[self.key] = {
                "body": self._buf.getvalue(),
                "size": len(self._buf.getvalue()),
                "content_type": "application/octet-stream",
                "updated": now,
            }
        self._buf.close()


class _FakeBlob:
    def __init__(self, store: dict, name: str):
        self.store = store
        self.name = name
        # Lazy: size/content_type/updated populated by reload() or list_blobs.
        self.size = None
        self.content_type = None
        self.updated = None

    def reload(self):
        if self.name not in self.store:
            raise gax_exc.NotFound(f"No such object: {self.name}")
        entry = self.store[self.name]
        self.size = entry["size"]
        self.content_type = entry["content_type"]
        self.updated = entry["updated"]

    def open(self, mode: str):
        return _FakeBlobFile(self.store, self.name, mode)


class _FakeBucket:
    def __init__(self, store: dict, name: str, exists: bool = True):
        self.store = store
        self.name = name
        self._exists = exists

    def list_blobs(self, prefix: str | None = None):
        for key, entry in sorted(self.store.items()):
            if prefix and not key.startswith(prefix):
                continue
            blob = _FakeBlob(self.store, key)
            blob.size = entry["size"]
            blob.content_type = entry["content_type"]
            blob.updated = entry["updated"]
            yield blob

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self.store, name)


class _FakeStorageClient:
    """Minimal google.cloud.storage.Client surrogate."""

    def __init__(self, store: dict, *, project: str | None = None, credentials=None):
        self.project = project
        self.credentials = credentials
        self._store = store
        self._buckets = {"test-bucket": _FakeBucket(store, "test-bucket")}

    def bucket(self, name: str) -> _FakeBucket:
        return self._buckets.setdefault(name, _FakeBucket(self._store, name))

    def get_bucket(self, name: str) -> _FakeBucket:
        if name not in self._buckets:
            raise gax_exc.NotFound(f"Bucket not found: {name}")
        return self._buckets[name]


# ----- Fixtures -----------------------------------------------------------


@pytest.fixture
def gcs_store() -> dict:
    """In-memory object store shared with the patched Client."""
    return {}


@pytest.fixture
def patched_gcs(monkeypatch, gcs_store):
    """Patch google.cloud.storage.Client and the service-account creds path.

    Returns the fake client class so tests can assert on construction
    if needed.
    """
    from google.cloud import storage

    def _make_client(project=None, credentials=None):
        return _FakeStorageClient(gcs_store, project=project, credentials=credentials)

    monkeypatch.setattr(storage, "Client", _make_client)

    # Stop service-account Credentials from trying to validate the dummy JSON.
    from google.oauth2 import service_account

    class _FakeCreds:
        pass

    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_info",
        classmethod(lambda cls, info: _FakeCreds()),
    )

    return _make_client


@pytest.fixture
def gcs_config() -> GCSConfig:
    # service_account_json is a SecretStr; pass a JSON string so the
    # SA-JSON branch in _build_gcs_client is exercised.
    return GCSConfig(
        bucket="test-bucket",
        project="test-project",
        service_account_json=json.dumps({"type": "service_account"}),
    )


# ----- Contract conformance (FileSource side) ----------------------------


class TestGCSFileSourceContract(FileSourceContract):
    @pytest.fixture
    def source(self, gcs_config, patched_gcs, gcs_store):
        now = datetime.now(timezone.utc)
        body = b"contract-fixture-body\n"
        gcs_store["fixture/hello.txt"] = {
            "body": body,
            "size": len(body),
            "content_type": "text/plain",
            "updated": now,
        }
        return GCSFileSource(gcs_config)

    @pytest.fixture
    def seeded_path(self):
        return "fixture/hello.txt"


# ----- Contract conformance (FileSink side) ------------------------------


class TestGCSFileSinkContract(FileSinkContract):
    @pytest.fixture
    def sink(self, gcs_config, patched_gcs):
        return GCSFileSink(gcs_config)

    @pytest.fixture
    def reader_for(self, gcs_store):
        def _read(path: str) -> bytes:
            return gcs_store[path]["body"]

        return _read


# ----- GCS-specific behavior ---------------------------------------------


class TestGCSSourceBehavior:
    def test_list_returns_content_type(self, gcs_config, patched_gcs, gcs_store):
        now = datetime.now(timezone.utc)
        body = b'{"a":1}'
        gcs_store["typed.json"] = {
            "body": body,
            "size": len(body),
            "content_type": "application/json",
            "updated": now,
        }
        source = GCSFileSource(gcs_config)
        meta = next(m for m in source.list() if m.path == "typed.json")
        assert meta.content_type == "application/json"
        assert meta.size == len(body)

    def test_head_returns_content_type(self, gcs_config, patched_gcs, gcs_store):
        now = datetime.now(timezone.utc)
        gcs_store["typed.json"] = {
            "body": b'{"a":1}',
            "size": 7,
            "content_type": "application/json",
            "updated": now,
        }
        meta = GCSFileSource(gcs_config).head("typed.json")
        assert meta.content_type == "application/json"

    def test_head_missing_raises_permanent(self, gcs_config, patched_gcs):
        with pytest.raises(PermanentError):
            GCSFileSource(gcs_config).head("never-existed.bin")

    def test_open_streams_full_body(self, gcs_config, patched_gcs, gcs_store):
        now = datetime.now(timezone.utc)
        body = b"streaming-gcs-body " * 200
        gcs_store["streamed.bin"] = {
            "body": body,
            "size": len(body),
            "content_type": "application/octet-stream",
            "updated": now,
        }
        chunks = list(GCSFileSource(gcs_config).open("streamed.bin"))
        assert b"".join(chunks) == body

    def test_prefix_in_config_scopes_listing(self, patched_gcs, gcs_store):
        now = datetime.now(timezone.utc)
        gcs_store["scoped/inside.txt"] = {
            "body": b"yes",
            "size": 3,
            "content_type": "text/plain",
            "updated": now,
        }
        gcs_store["outside.txt"] = {
            "body": b"no",
            "size": 2,
            "content_type": "text/plain",
            "updated": now,
        }
        cfg = GCSConfig(
            bucket="test-bucket",
            prefix="scoped",
            project="test-project",
            service_account_json=json.dumps({"type": "service_account"}),
        )
        listing = [m.path for m in GCSFileSource(cfg).list()]
        assert "scoped/inside.txt" in listing
        assert "outside.txt" not in listing

    def test_check_missing_bucket_returns_not_ok(self, patched_gcs):
        cfg = GCSConfig(
            bucket="never-created",
            project="test-project",
            service_account_json=json.dumps({"type": "service_account"}),
        )
        result = GCSFileSource(cfg).check()
        assert result.ok is False
        assert result.detail

    def test_check_existing_bucket_returns_ok(self, gcs_config, patched_gcs):
        result = GCSFileSource(gcs_config).check()
        assert result.ok is True


class TestGCSSinkBehavior:
    def test_write_round_trip(self, gcs_config, patched_gcs, gcs_store):
        body = b"sink-round-trip\n"
        sink = GCSFileSink(gcs_config)
        result = sink.write("written.txt", iter([body]))
        assert result.path == "written.txt"
        assert result.bytes_written == len(body)
        assert gcs_store["written.txt"]["body"] == body

    def test_write_multi_chunk(self, gcs_config, patched_gcs, gcs_store):
        chunks = [b"part-1\n", b"part-2\n", b"part-3\n"]
        GCSFileSink(gcs_config).write("multi.txt", iter(chunks))
        assert gcs_store["multi.txt"]["body"] == b"".join(chunks)

    def test_prefix_in_config_joined_to_write(self, patched_gcs, gcs_store):
        cfg = GCSConfig(
            bucket="test-bucket",
            prefix="outbox",
            project="test-project",
            service_account_json=json.dumps({"type": "service_account"}),
        )
        result = GCSFileSink(cfg).write("subdir/file.txt", iter([b"prefixed"]))
        assert result.path == "outbox/subdir/file.txt"
        assert gcs_store["outbox/subdir/file.txt"]["body"] == b"prefixed"

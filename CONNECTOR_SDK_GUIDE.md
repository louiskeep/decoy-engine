# Connector SDK guide

> **Status:** shipped 2026-05-10 (Sprint G Weeks 1-6).
> **Last reviewed:** 2026-05-10.
> **Companion docs:** [`CONNECTOR_SDK_CONTRACT.md`](CONNECTOR_SDK_CONTRACT.md) covers
> the legacy table-shaped contract (load/save returning `pyarrow.Table`).
> This guide is the tutorial for the new file-shaped SDK (`FileSource`,
> `FileSink`) that Sprint G shipped.

## What this SDK is for

You have a cloud-storage system that Decoy doesn't ship a first-party
connector for: Azure Blob, Box, Dropbox, OneDrive, a vendor blob store,
or an internal file system. You want your pipelines to read from it and
write to it without forking the engine.

The SDK gives you two abstract base classes (`FileSource` for reads,
`FileSink` for writes) plus the types and exception machinery. You write
a class for each side that needs about 50 lines of code, package it as a
pip-installable wheel, and the engine picks it up via setuptools entry
points the next time it starts.

The first-party connectors that shipped in Sprint G (`S3FileSource`,
`GCSFileSource`, `SFTPFileSource` and their sinks) use the exact same
SDK. There is no two-tier system: what you can build externally has the
same surface area as what we ship.

## Quick install

```bash
pip install decoy-engine                # the SDK is in the base install
pip install decoy-engine[sftp]           # if you also want first-party SFTP
pip install decoy-engine[gcs]            # if you also want first-party GCS
```

The SDK lives at `decoy_engine.sdk`. The top-level package re-exports
the names so `from decoy_engine import FileSource, FileSink, ...` also
works.

## The contract in one screen

```python
from decoy_engine.sdk import FileSource, FileSink, ConnectorConfig

class MyConfig(ConnectorConfig):
    """Pydantic model. HiFi auto-renders the form from this."""
    # ... your fields ...

class MySource(FileSource[MyConfig]):
    name = "my_source"               # short stable identifier
    version = "1.0.0"                # semver of YOUR connector
    min_sdk_version = "1.0"          # minimum SDK version you need
    capabilities = {                 # what you can do
        "supports_streaming": True,
        "supports_introspection": True,
    }
    def check(self) -> CheckResult: ...
    def list(self, prefix: str | None = None) -> Iterator[FileMeta]: ...
    def head(self, path: str) -> FileMeta: ...
    def open(self, path: str) -> Iterator[bytes]: ...
    def close(self) -> None: ...

class MySink(FileSink[MyConfig]):
    name = "my_source"
    version = "1.0.0"
    capabilities = {"supports_streaming": True}
    def check(self) -> CheckResult: ...
    def write(self, path: str, chunks: Iterator[bytes]) -> WriteResult: ...
    def close(self) -> None: ...
```

That's the whole surface. Five methods on the source side, two on the
sink side. The base classes inherit `ABCMeta` so an incomplete subclass
fails to instantiate, not at first method call.

## Worked example: an Azure Blob connector

The example below is a complete, working community connector for Azure
Blob Storage. It uses only the public SDK; nothing imports
`decoy_engine.internal` or any private symbol.

### Project layout

A community connector ships as its own pip-installable package:

```
decoy-connector-azure/
├── pyproject.toml
├── README.md
└── src/decoy_connector_azure/
    ├── __init__.py
    └── azure_blob.py
```

`pyproject.toml`:

```toml
[project]
name = "decoy-connector-azure"
version = "0.1.0"
description = "Azure Blob Storage connector for Decoy"
requires-python = ">=3.10"
dependencies = [
    "decoy-engine>=1.0",
    "azure-storage-blob>=12.0",
]

[project.entry-points."decoy.connectors"]
azure_blob_source = "decoy_connector_azure.azure_blob:AzureBlobFileSource"
azure_blob_sink   = "decoy_connector_azure.azure_blob:AzureBlobFileSink"
```

The two `decoy.connectors` entry points are what the engine reads at
startup to auto-discover the connector. The point names (`azure_blob_source`,
`azure_blob_sink`) can be whatever you want; the engine reads the class
they point to.

### The connector code

`src/decoy_connector_azure/azure_blob.py`:

```python
"""Azure Blob Storage connector for Decoy.

Implements FileSource + FileSink against the azure-storage-blob v12 SDK.
Auth via connection string (simplest path); production deploys should
use Managed Identity or Workload Identity Credentials instead.
"""
from __future__ import annotations

from typing import ClassVar, Iterator, Optional

from pydantic import Field, SecretStr

from decoy_engine.sdk import (
    CAP_INTROSPECTION,
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


_DEFAULT_CHUNK_BYTES = 1 * 1024 * 1024


class AzureBlobConfig(ConnectorConfig):
    """Config for AzureBlobFileSource / AzureBlobFileSink."""

    container: str = Field(..., min_length=1, max_length=63)
    prefix: str = ""
    # Connection string. For production use Azure Managed Identity
    # via DefaultAzureCredential instead; this is the simplest path
    # for getting started.
    connection_string: SecretStr


def _wrap_azure_error(exc: Exception) -> Exception:
    """Translate azure-storage-blob exceptions into typed SDK errors."""
    try:
        from azure.core.exceptions import (
            ClientAuthenticationError,
            HttpResponseError,
            ResourceNotFoundError,
            ServiceRequestError,
        )
    except ImportError:
        return PermanentError(f"Unexpected error (azure SDK missing): {exc}")

    if isinstance(exc, ResourceNotFoundError):
        return PermanentError(f"Azure blob not found: {exc}")
    if isinstance(exc, ClientAuthenticationError):
        return PermanentError(f"Azure auth failed: {exc}")
    if isinstance(exc, ServiceRequestError):
        return TransientError(f"Azure transient error: {exc}")
    if isinstance(exc, HttpResponseError):
        if exc.status_code and 500 <= exc.status_code < 600:
            return TransientError(f"Azure 5xx: {exc}")
        return PermanentError(f"Azure {exc.status_code or '?'}: {exc}")
    return PermanentError(f"Unexpected Azure error: {exc}")


def _build_service_client(config: AzureBlobConfig):
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient.from_connection_string(
        config.connection_string.get_secret_value()
    )


def _join_key(prefix: str, path: str) -> str:
    p = (prefix or "").rstrip("/")
    k = (path or "").lstrip("/")
    return f"{p}/{k}" if p else k


class AzureBlobFileSource(FileSource[AzureBlobConfig]):
    """Read blobs from an Azure Blob Storage container."""

    name: ClassVar[str] = "azure_blob"
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
        CAP_INTROSPECTION: True,
    }

    def __init__(self, config: AzureBlobConfig) -> None:
        super().__init__(config)
        self._container = None

    def _container_client(self):
        if self._container is None:
            svc = _build_service_client(self.config)
            self._container = svc.get_container_client(self.config.container)
        return self._container

    def check(self) -> CheckResult:
        try:
            self._container_client().get_container_properties()
        except Exception as exc:
            return CheckResult(ok=False, detail=str(_wrap_azure_error(exc)))
        return CheckResult(ok=True)

    def list(self, prefix: Optional[str] = None) -> Iterator[FileMeta]:
        client = self._container_client()
        effective = _join_key(self.config.prefix, prefix or "")
        try:
            for blob in client.list_blobs(name_starts_with=effective or None):
                yield FileMeta(
                    path=blob.name,
                    size=blob.size,
                    content_type=(
                        blob.content_settings.content_type
                        if blob.content_settings
                        else None
                    ),
                    modified=(
                        blob.last_modified.isoformat()
                        if blob.last_modified
                        else None
                    ),
                )
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc

    def head(self, path: str) -> FileMeta:
        client = self._container_client()
        try:
            props = client.get_blob_client(path).get_blob_properties()
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
        return FileMeta(
            path=path,
            size=props.size,
            content_type=(
                props.content_settings.content_type
                if props.content_settings
                else None
            ),
            modified=(
                props.last_modified.isoformat() if props.last_modified else None
            ),
        )

    def open(self, path: str) -> Iterator[bytes]:
        client = self._container_client().get_blob_client(path)
        try:
            stream = client.download_blob()
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
        try:
            for chunk in stream.chunks():
                if chunk:
                    yield chunk
        finally:
            # Azure's download stream has no public close(); GC handles it.
            pass


class AzureBlobFileSink(FileSink[AzureBlobConfig]):
    """Write blobs to an Azure Blob Storage container."""

    name: ClassVar[str] = "azure_blob"
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
    }

    def __init__(self, config: AzureBlobConfig) -> None:
        super().__init__(config)
        self._container = None

    def _container_client(self):
        if self._container is None:
            svc = _build_service_client(self.config)
            self._container = svc.get_container_client(self.config.container)
        return self._container

    def check(self) -> CheckResult:
        try:
            self._container_client().get_container_properties()
        except Exception as exc:
            return CheckResult(ok=False, detail=str(_wrap_azure_error(exc)))
        return CheckResult(ok=True)

    def write(self, path: str, chunks: Iterator[bytes]) -> WriteResult:
        client = self._container_client()
        key = _join_key(self.config.prefix, path)
        # Buffer once; azure-storage-blob's upload_blob accepts an
        # iterable but the API is friendlier with bytes for sub-100MB
        # writes. Larger writes should use upload_blob with chunked
        # streaming and put_block / put_block_list directly, but that's
        # left as an exercise for production-grade implementations.
        body = bytearray()
        for chunk in chunks:
            if chunk:
                body.extend(chunk)
        try:
            client.get_blob_client(key).upload_blob(
                bytes(body), overwrite=True
            )
        except Exception as exc:
            raise _wrap_azure_error(exc) from exc
        return WriteResult(path=key, bytes_written=len(body))
```

About 160 lines including imports + docstrings. Production hardening
(multipart for large writes, signed-URL generation, content-type hints)
adds another 100 lines or so. The skeleton above passes the standard
contract test suite.

### Testing your connector

The SDK ships reusable contract test base classes that work for any
`FileSource` / `FileSink` implementation. Drop one fixture and you
inherit the full contract suite:

```python
# tests/test_azure_blob.py
import pytest
from decoy_connector_azure.azure_blob import (
    AzureBlobConfig,
    AzureBlobFileSink,
    AzureBlobFileSource,
)
from decoy_engine.sdk import PermanentError

# Inherit the SDK's contract suite. Both these are exposed at
# tests/connectors/sdk_contract_tests.py in the engine repo when you
# install decoy-engine[dev]; copy them into your own test tree if your
# CI doesn't pull engine dev extras.
from sdk_contract_tests import FileSinkContract, FileSourceContract


class TestSource(FileSourceContract):
    @pytest.fixture
    def source(self, seeded_azurite_container):
        # azurite is Microsoft's local Azure Blob emulator; spin it up
        # in CI like moto for S3. See README in this connector package
        # for the docker-compose snippet.
        return AzureBlobFileSource(AzureBlobConfig(
            container="test",
            connection_string="DefaultEndpointsProtocol=...",
        ))

    @pytest.fixture
    def seeded_path(self):
        return "fixture/hello.txt"


class TestSink(FileSinkContract):
    @pytest.fixture
    def sink(self, seeded_azurite_container):
        return AzureBlobFileSink(AzureBlobConfig(...))

    @pytest.fixture
    def reader_for(self, seeded_azurite_container):
        def _read(path: str) -> bytes:
            # Use the azure SDK directly to read back what the sink wrote.
            ...
        return _read
```

The inherited contract tests check: ABC inheritance, declared metadata
(name / version / min_sdk_version), `check()` returns `CheckResult`,
`list()` yields `FileMeta`, `head()` works for an existing path and
raises `ConnectorError` for a missing one, `open()` yields bytes,
write round-trip works, multipart round-trip if you advertise that
capability, and a few more. Roughly 20 tests inherited, zero written
locally for the structural contract.

Add per-connector behavior tests on top for anything specific to your
service (Azure-specific edge cases, content-type handling, etc.).

## Capability flags

Set these as keys in your `capabilities` dict so the engine routes to
the optimal code path at runtime. The flag constants live in
`decoy_engine.sdk`.

| Flag                   | When to set it                                          |
| ---                    | ---                                                     |
| `CAP_STREAMING`        | Your source can yield byte chunks instead of full file. |
| `CAP_RESUMABLE`        | Your source can resume from an offset (Range header).   |
| `CAP_SIGNED_URL`       | You can mint signed URLs for direct browser uploads.    |
| `CAP_MULTIPART`        | Your sink supports parallel multipart uploads.          |
| `CAP_INTROSPECTION`    | `list()` includes size + content-type on each FileMeta. |
| `CAP_DRY_RUN`          | You can validate the config without committing writes.  |

The engine uses these to pick paths. A source with `CAP_STREAMING=True`
gets streaming reads; without it the runner falls back to full-buffer
reads at op boundaries. Adding new flags later is additive: old
connectors keep working with whatever flags they advertise.

## Exception types

Three exception types from `decoy_engine.sdk`. Raise the right one:

* `TransientError` for retryable failures (rate limits, 5xx upstream,
  network blips). The engine retries with exponential backoff.
* `PermanentError` for non-recoverable runtime failures (missing
  object, auth denied, quota exceeded). The engine surfaces it
  immediately.
* `ConfigError` for bad config caught at `check()` time. The HiFi UI
  surfaces this as a red banner on the config form before the user
  starts a job. Use this when the issue is in the form, not in the
  remote system.

Any other exception leaking from your code gets wrapped as
`PermanentError(original=e)` by the runner. That's safe but loses
specificity; raising one of the three types above is better.

## Versioning + min_sdk_version

Your connector class declares two version strings:

* `version`: your connector's semver. Bump it on behavior changes.
* `min_sdk_version`: the minimum SDK version your code needs. The
  engine compares this against its installed `SDK_VERSION` at
  connector-load time. If the SDK is older than `min_sdk_version`, the
  connector refuses to load with a clear admin-facing error rather than
  failing mid-pipeline.

If you don't change the contract you're calling, leave `min_sdk_version`
alone. If you start using a new capability flag or a method that
appeared in a later SDK release, bump `min_sdk_version` to match.

## Packaging summary

To ship a community connector:

1. Make it a normal pip package (`pyproject.toml` with one or two
   `decoy.connectors` entry points).
2. Depend on `decoy-engine>=1.0` (or whatever your `min_sdk_version`
   requires).
3. Add per-connector contract tests inheriting from
   `FileSourceContract` / `FileSinkContract`.
4. Publish to PyPI (or to your private index for internal connectors).

Customers install with `pip install your-package`; the engine picks it
up at the next start. No engine fork, no PR to upstream Decoy.

## Where this fits on the roadmap

* **Sprint G Week 1 (shipped):** SDK contract locked.
* **Sprint G Weeks 2-3 (shipped):** First-party connectors (S3, GCS, SFTP).
* **Sprint G Week 6 (this doc):** External community-connector tutorial
  + worked example.
* **Future:** A community connector registry / discovery surface. Not
  yet planned; today users just `pip install` whatever they want.

"""End-to-end verification that the public SDK (`decoy_engine.sdk`) is
sufficient to build a third-party file connector without touching any
engine internals.

The "community connector" implemented here is intentionally minimal: it
operates against an in-memory `dict[str, bytes]` instead of a real cloud
service. The point isn't to test cloud-storage code; it's to prove that
the public surface is genuinely self-contained. If a future SDK change
breaks this test, it means a community connector somewhere would also
break. That's the regression we want to catch.

Imports the test class only from `decoy_engine.sdk` and the contract
test classes from the contract suite (the same way a real community
connector author would in their own repo).
"""
from __future__ import annotations

from typing import ClassVar, Iterator, Optional

import pytest
from pydantic import Field

# Public SDK only. If you have to add a `from decoy_engine.internal...`
# import to make this file work, the SDK has a leaky abstraction.
from decoy_engine.sdk import (
    CAP_INTROSPECTION,
    CAP_STREAMING,
    CheckResult,
    ConnectorConfig,
    FileMeta,
    FileSink,
    FileSource,
    PermanentError,
    WriteResult,
)

from sdk_contract_tests import FileSinkContract, FileSourceContract  # noqa: E402


# ----- The community connector --------------------------------------------


class _InMemoryConfig(ConnectorConfig):
    """A trivial connector config; mirrors the shape an external author
    would write."""

    namespace: str = Field(..., min_length=1)


# Module-level store so source and sink share state within a test.
# A real connector wouldn't do this; they'd talk to a network service.
_STORE: dict[str, dict[str, bytes]] = {}


class _CommunitySource(FileSource[_InMemoryConfig]):
    """Read files from an in-memory store. Stands in for an Azure Blob /
    Box / Dropbox / etc. connector that someone outside Decoy might
    write."""

    name: ClassVar[str] = "community_inmem"
    version: ClassVar[str] = "0.1.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
        CAP_INTROSPECTION: True,
    }

    def check(self) -> CheckResult:
        # The namespace must exist; an unknown namespace is a "remote
        # location not reachable" condition (CheckResult ok=False), not
        # a config error (the config itself is well-formed).
        if self.config.namespace not in _STORE:
            return CheckResult(ok=False, detail="namespace not initialized")
        return CheckResult(ok=True)

    def list(self, prefix: Optional[str] = None) -> Iterator[FileMeta]:
        bucket = _STORE.get(self.config.namespace, {})
        for path, body in sorted(bucket.items()):
            if prefix and not path.startswith(prefix):
                continue
            yield FileMeta(path=path, size=len(body), content_type=None)

    def open(self, path: str) -> Iterator[bytes]:
        bucket = _STORE.get(self.config.namespace, {})
        if path not in bucket:
            raise PermanentError(f"community_inmem: not found: {path}")
        # Chunk it manually to exercise the streaming path.
        body = bucket[path]
        for i in range(0, len(body), 4):
            yield body[i : i + 4]


class _CommunitySink(FileSink[_InMemoryConfig]):
    """Write files to the same in-memory store."""

    name: ClassVar[str] = "community_inmem"
    version: ClassVar[str] = "0.1.0"
    capabilities: ClassVar[dict[str, bool]] = {
        CAP_STREAMING: True,
    }

    def check(self) -> CheckResult:
        return CheckResult(ok=True)

    def write(self, path: str, chunks: Iterator[bytes]) -> WriteResult:
        bucket = _STORE.setdefault(self.config.namespace, {})
        body = bytearray()
        for chunk in chunks:
            if chunk:
                body.extend(chunk)
        bucket[path] = bytes(body)
        return WriteResult(path=path, bytes_written=len(body))


# ----- Contract conformance ----------------------------------------------


@pytest.fixture
def fresh_store():
    """Reset the shared store before each test so they don't leak state."""
    _STORE.clear()
    yield _STORE
    _STORE.clear()


class TestCommunitySourceContract(FileSourceContract):
    @pytest.fixture
    def source(self, fresh_store):
        fresh_store.setdefault("ns1", {})["hello.txt"] = b"community fixture body\n"
        return _CommunitySource(_InMemoryConfig(namespace="ns1"))

    @pytest.fixture
    def seeded_path(self):
        return "hello.txt"


class TestCommunitySinkContract(FileSinkContract):
    @pytest.fixture
    def sink(self, fresh_store):
        return _CommunitySink(_InMemoryConfig(namespace="ns1"))

    @pytest.fixture
    def reader_for(self, fresh_store):
        def _read(path: str) -> bytes:
            return fresh_store["ns1"][path]
        return _read


# ----- The actual proof: end-to-end without touching internals ------------


class TestEndToEndThroughPublicSDKOnly:
    """Wire the in-memory community connectors end-to-end using nothing
    but public SDK names. If this test passes, an external author can
    build a connector against the same surface."""

    def test_write_then_read_round_trip(self, fresh_store):
        sink = _CommunitySink(_InMemoryConfig(namespace="round-trip"))
        body = b"first chunk\nsecond chunk\nthird chunk\n"
        result = sink.write("data/out.csv", iter([body[:11], body[11:23], body[23:]]))
        assert result.bytes_written == len(body)
        assert result.path == "data/out.csv"

        source = _CommunitySource(_InMemoryConfig(namespace="round-trip"))
        chunks = list(source.open("data/out.csv"))
        assert b"".join(chunks) == body

    def test_list_then_head_works_via_default_implementation(self, fresh_store):
        # FileSource.head() falls back to walking list() when a connector
        # doesn't override it. Verify the default works for a community
        # connector that didn't bother to override.
        fresh_store.setdefault("ns", {})["a.csv"] = b"abc"
        fresh_store["ns"]["b.csv"] = b"defg"
        source = _CommunitySource(_InMemoryConfig(namespace="ns"))
        meta = source.head("b.csv")
        assert meta.path == "b.csv"
        assert meta.size == 4

    def test_head_missing_raises_permanent(self, fresh_store):
        fresh_store["ns"] = {}
        source = _CommunitySource(_InMemoryConfig(namespace="ns"))
        with pytest.raises(PermanentError):
            source.head("missing.csv")

    def test_capability_dict_is_readable_by_consumers(self):
        # An engine consumer reads the capability dict to pick a code
        # path. Validate the public class attribute is plain Python and
        # contains the SDK constant keys.
        caps = _CommunitySource.capabilities
        assert isinstance(caps, dict)
        assert caps.get(CAP_STREAMING) is True
        assert caps.get(CAP_INTROSPECTION) is True

    def test_min_sdk_version_inheritance(self):
        # A connector that doesn't override min_sdk_version inherits
        # the SDK version it was compiled against. The engine uses this
        # to gate loading.
        from decoy_engine.sdk import SDK_VERSION

        assert _CommunitySource.min_sdk_version == SDK_VERSION
        assert _CommunitySink.min_sdk_version == SDK_VERSION


class TestPublicSurfaceCompleteness:
    """Belt-and-braces check that the names a community connector author
    needs are all importable from `decoy_engine.sdk`. The list mirrors
    the imports at the top of this file plus the names cited in
    CONNECTOR_SDK_GUIDE.md.

    If any of these become inaccessible from `decoy_engine.sdk`, an
    external community connector breaks. This test exists to catch
    that regression."""

    def test_all_documented_names_importable(self):
        import decoy_engine.sdk as sdk

        required = [
            # Capability flag constants
            "CAP_STREAMING",
            "CAP_RESUMABLE",
            "CAP_SIGNED_URL",
            "CAP_MULTIPART",
            "CAP_INTROSPECTION",
            "CAP_DRY_RUN",
            # Value types
            "ConnectorConfig",
            "FileMeta",
            "CheckResult",
            "WriteResult",
            # Abstract bases
            "FileSource",
            "FileSink",
            # Exceptions
            "TransientError",
            "PermanentError",
            # Version
            "SDK_VERSION",
        ]
        for name in required:
            assert hasattr(sdk, name), f"{name!r} missing from decoy_engine.sdk"

    def test_top_level_alias_also_exposes_them(self):
        # Customers can also `from decoy_engine import FileSource` rather
        # than the submodule path. Both should resolve to the same class
        # objects.
        from decoy_engine import FileSink as TopSink
        from decoy_engine import FileSource as TopSource
        from decoy_engine.sdk import FileSink as SubSink
        from decoy_engine.sdk import FileSource as SubSource

        assert TopSource is SubSource
        assert TopSink is SubSink

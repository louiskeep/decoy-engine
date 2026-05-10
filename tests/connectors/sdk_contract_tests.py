"""Contract tests that every `FileSource` and `FileSink` implementation
must pass.

This module ships in the engine's tests/ tree so first-party connectors
can import it. Community connectors should also depend on `decoy-engine`
in their dev-extras and import these classes the same way.

Usage pattern:

    # tests/connectors/test_my_source.py
    import pytest
    from decoy_engine.sdk import FileSource
    from my_pkg.my_source import MySource, MySourceConfig
    from tests.connectors.sdk_contract_tests import FileSourceContract

    class TestMySource(FileSourceContract):
        @pytest.fixture
        def source(self, my_fixture):
            return MySource(MySourceConfig(...))

        @pytest.fixture
        def seeded_path(self, my_fixture):
            return "fixture-seeded-file.txt"

The base classes intentionally avoid asserting on the contents of the
remote location: each connector test provides a `source` (or `sink`)
fixture configured against its own moto / mock-gcs / paramiko-server.
The contract tests check shape and protocol compliance, not data.
"""
from __future__ import annotations

import pytest

from decoy_engine.sdk import (
    CAP_INTROSPECTION,
    CAP_MULTIPART,
    CAP_STREAMING,
    CheckResult,
    ConnectorError,
    FileMeta,
    FileSink,
    FileSource,
    SDK_VERSION,
    WriteResult,
)


class FileSourceContract:
    """Subclass and provide a `source` fixture yielding a configured
    `FileSource` instance.

    Override `seeded_path` if your fixture seeds a file the contract test
    can read. Override `optional_test_*` methods to opt into the extra
    checks specific to your connector's capabilities.
    """

    # ----- Fixtures the subclass must provide ----------------------------

    @pytest.fixture
    def source(self) -> FileSource:
        raise NotImplementedError(
            "FileSourceContract subclass must provide a `source` fixture"
        )

    @pytest.fixture
    def seeded_path(self) -> str | None:
        """Path the subclass's fixture seeded into the remote location.

        Return None if the fixture cannot pre-seed (rare). Tests that need
        a real file will pytest.skip in that case.
        """
        return None

    # ----- Static contract: metadata + protocol --------------------------

    def test_inherits_filesource(self, source):
        assert isinstance(source, FileSource), (
            f"Connector {type(source).__name__} must inherit FileSource"
        )

    def test_name_is_declared(self, source):
        assert type(source).name, (
            "FileSource subclass must declare a non-empty `name` class attr"
        )

    def test_version_is_declared(self, source):
        assert type(source).version, (
            "FileSource subclass must declare a non-empty `version` class attr"
        )

    def test_min_sdk_version_compatible(self, source):
        # min_sdk_version <= SDK_VERSION at runtime, else engine refuses to load.
        # A connector that ships with a future-dated min_sdk_version is a bug.
        min_v = type(source).min_sdk_version
        assert min_v, "min_sdk_version must be a non-empty string"
        assert min_v <= SDK_VERSION, (
            f"Connector requires SDK {min_v}, installed SDK is {SDK_VERSION}"
        )

    def test_capabilities_is_dict(self, source):
        caps = type(source).capabilities
        assert isinstance(caps, dict)
        for k, v in caps.items():
            assert isinstance(k, str)
            assert isinstance(v, bool), (
                f"Capability `{k}` must be bool, got {type(v).__name__}"
            )

    # ----- Runtime contract: check + list + open -------------------------

    def test_check_returns_check_result(self, source):
        result = source.check()
        assert isinstance(result, CheckResult), (
            f"check() must return CheckResult, got {type(result).__name__}"
        )

    def test_list_yields_file_meta(self, source):
        # We can't assert non-empty here: some fixture setups list an empty bucket.
        for item in source.list():
            assert isinstance(item, FileMeta), (
                f"list() must yield FileMeta, got {type(item).__name__}"
            )
            assert item.path, "FileMeta.path must be non-empty"

    def test_open_yields_bytes_when_path_exists(self, source, seeded_path):
        if not seeded_path:
            pytest.skip("Fixture did not seed a file path for open() test")
        chunks = list(source.open(seeded_path))
        for chunk in chunks:
            assert isinstance(chunk, (bytes, bytearray)), (
                f"open() must yield bytes-like chunks, got {type(chunk).__name__}"
            )
        # Roundtripping yields at least one chunk for a non-empty seeded file.
        assert chunks, "open() of a real path yielded zero chunks"

    def test_open_missing_path_raises_connector_error(self, source):
        with pytest.raises(ConnectorError):
            # Force eager iteration; some connectors only fail on first chunk.
            list(source.open("does-not-exist-" + "x" * 20))

    # ----- Capability-gated tests ----------------------------------------

    def test_introspection_supplies_size_when_advertised(self, source, seeded_path):
        # If the connector claims supports_introspection, listed FileMeta
        # entries must include size where available.
        if not type(source).capabilities.get(CAP_INTROSPECTION):
            pytest.skip("Connector does not advertise supports_introspection")
        if not seeded_path:
            pytest.skip("Fixture did not seed a file for introspection check")
        meta = next(
            (m for m in source.list() if m.path == seeded_path),
            None,
        )
        assert meta is not None, f"Seeded path {seeded_path!r} not present in list()"
        assert meta.size is not None, (
            "supports_introspection=True requires FileMeta.size on listed files"
        )


class FileSinkContract:
    """Subclass and provide a `sink` fixture yielding a configured
    `FileSink` instance.

    Override `reader_for` if your sink has a paired source for round-trip
    verification (the common pattern: same connector class implements both
    sides, so the test reads back what it wrote).
    """

    # ----- Fixtures the subclass must provide ----------------------------

    @pytest.fixture
    def sink(self) -> FileSink:
        raise NotImplementedError(
            "FileSinkContract subclass must provide a `sink` fixture"
        )

    @pytest.fixture
    def reader_for(self):
        """Optional callable `(path) -> bytes` for round-trip verification.

        Default returns None; round-trip tests will pytest.skip. Override
        when the sink's fixture also exposes a way to read what was just
        written (typical: same moto / mock backend).
        """
        return None

    # ----- Static contract -----------------------------------------------

    def test_inherits_filesink(self, sink):
        assert isinstance(sink, FileSink), (
            f"Connector {type(sink).__name__} must inherit FileSink"
        )

    def test_name_is_declared(self, sink):
        assert type(sink).name

    def test_version_is_declared(self, sink):
        assert type(sink).version

    def test_min_sdk_version_compatible(self, sink):
        min_v = type(sink).min_sdk_version
        assert min_v and min_v <= SDK_VERSION

    def test_capabilities_is_dict(self, sink):
        assert isinstance(type(sink).capabilities, dict)

    # ----- Runtime contract: check + write + round-trip ------------------

    def test_check_returns_check_result(self, sink):
        assert isinstance(sink.check(), CheckResult)

    def test_write_returns_write_result(self, sink):
        body = b"contract-test-body\n"
        result = sink.write("decoy-contract-test.txt", iter([body]))
        assert isinstance(result, WriteResult)
        assert result.bytes_written == len(body)

    def test_write_round_trip(self, sink, reader_for):
        if reader_for is None:
            pytest.skip(
                "FileSinkContract subclass did not provide `reader_for` fixture"
            )
        body = b"hello round trip\n"
        result = sink.write("decoy-contract-roundtrip.txt", iter([body]))
        got = reader_for(result.path)
        assert got == body, (
            f"Round-trip mismatch: wrote {body!r}, read back {got!r}"
        )

    def test_multipart_round_trip_when_advertised(self, sink, reader_for):
        # A multipart-capable sink must accept a multi-chunk iterator and
        # produce a byte-exact roundtrip equal to the chunks concatenated.
        if not type(sink).capabilities.get(CAP_MULTIPART):
            pytest.skip("Sink does not advertise supports_multipart")
        if reader_for is None:
            pytest.skip("Need reader_for fixture for round-trip verification")
        chunks = [b"chunk-1\n", b"chunk-2\n", b"chunk-3\n"]
        result = sink.write("decoy-contract-multipart.txt", iter(chunks))
        got = reader_for(result.path)
        assert got == b"".join(chunks)


# ----- Sentinel: SDK_VERSION sanity --------------------------------------

def test_sdk_version_is_declared_and_parseable():
    """Belt-and-braces check that the module-level SDK_VERSION constant
    actually parses as a version string. Cheap; runs even when no connectors
    are loaded.
    """
    assert SDK_VERSION
    major, _, _ = SDK_VERSION.partition(".")
    assert major.isdigit(), f"SDK_VERSION major must be numeric, got {SDK_VERSION!r}"


def test_capability_flag_constants_are_strings():
    """The CAP_* constants are the dictionary keys connectors set in their
    `capabilities` attribute. Anything non-string here would be a typo bug
    that breaks every connector silently.
    """
    from decoy_engine import sdk

    for name in dir(sdk):
        if name.startswith("CAP_"):
            value = getattr(sdk, name)
            assert isinstance(value, str), (
                f"sdk.{name} must be a string, got {type(value).__name__}"
            )
            assert value.startswith("supports_"), (
                f"sdk.{name} value must start with 'supports_', got {value!r}"
            )

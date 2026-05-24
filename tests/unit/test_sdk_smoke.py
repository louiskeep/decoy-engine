"""Smoke tests for the public connector SDK surface.

These run without any real connector implementation: they validate the
ABCs themselves, the exception hierarchy, the capability-flag constants,
and that the SDK exports are reachable both from `decoy_engine` and
`decoy_engine.sdk`. Per-connector behavior tests live alongside each
connector implementation (Week 2-3 of Sprint G).
"""

from __future__ import annotations

import pytest

from decoy_engine import sdk
from decoy_engine.errors import ConnectorError as LegacyConnectorError


class TestPublicSurface:
    """Names that contributors and connector authors are guaranteed to find."""

    def test_top_level_imports_work(self):
        # `from decoy_engine import FileSource, FileSink, ...` is the
        # documented import shape; if these vanish, every connector breaks.
        from decoy_engine import (
            SDK_VERSION,
            CheckResult,
            ConnectorConfig,
            FileMeta,
            FileSink,
            FileSource,
            PermanentError,
            TransientError,
            WriteResult,
        )

        assert SDK_VERSION
        assert all(
            cls is not None
            for cls in [
                CheckResult,
                ConnectorConfig,
                FileMeta,
                FileSink,
                FileSource,
                PermanentError,
                TransientError,
                WriteResult,
            ]
        )

    def test_submodule_imports_work(self):
        from decoy_engine.sdk import SDK_VERSION, FileSink, FileSource

        assert FileSource and FileSink and SDK_VERSION

    def test_top_level_and_submodule_export_same_classes(self):
        # FileSource imported either way must be the same class, else
        # `isinstance(x, FileSource)` will silently miss valid connectors.
        from decoy_engine import FileSource as Top
        from decoy_engine.sdk import FileSource as Sub

        assert Top is Sub


class TestExceptionHierarchy:
    """SDK errors compose cleanly into the existing engine exception tree."""

    def test_transient_error_is_a_connector_error(self):
        # Engine-level handlers that already catch ConnectorError must
        # also catch TransientError automatically.
        assert issubclass(sdk.TransientError, LegacyConnectorError)

    def test_permanent_error_is_a_connector_error(self):
        assert issubclass(sdk.PermanentError, LegacyConnectorError)

    def test_config_error_is_NOT_a_connector_error(self):
        # ConfigError is fix-the-form, not fix-the-network; retry logic
        # for connector errors must not silently swallow it.
        assert not issubclass(sdk.ConfigError, LegacyConnectorError)

    def test_raising_transient_caught_as_connector_error(self):
        with pytest.raises(LegacyConnectorError):
            raise sdk.TransientError("blip")


class TestAbstractEnforcement:
    """Concrete connectors must implement the abstract methods."""

    def test_filesource_cannot_be_instantiated_directly(self):
        # ABCMeta enforces this; trying to instantiate FileSource itself
        # is a programmer error and should fail at construction time, not
        # at first method call.
        with pytest.raises(TypeError):
            sdk.FileSource(None)  # type: ignore[abstract]

    def test_filesink_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            sdk.FileSink(None)  # type: ignore[abstract]

    def test_partial_implementation_fails_at_instantiation(self):
        # A subclass that fails to implement an @abstractmethod is itself
        # still abstract; instantiation should fail.
        class Partial(sdk.FileSource):
            name = "partial"
            version = "0.0.1"

            def check(self):
                return sdk.CheckResult(ok=True)

            # Deliberately missing: list, open

        with pytest.raises(TypeError):
            Partial(None)  # type: ignore[abstract]

    def test_full_implementation_can_be_instantiated(self):
        # A subclass implementing every abstractmethod constructs cleanly.
        class FullSource(sdk.FileSource):
            name = "test_full"
            version = "1.0.0"

            def check(self):
                return sdk.CheckResult(ok=True)

            def list(self, prefix=None):
                return iter([])

            def open(self, path):
                return iter([])

        instance = FullSource(None)  # type: ignore[arg-type]
        assert isinstance(instance, sdk.FileSource)
        assert instance.check().ok is True


class TestCapabilityFlags:
    """Capability constants are stable strings; new ones added later are
    additive and don't break existing connectors."""

    def test_known_capabilities_are_supports_prefixed(self):
        for name in [
            "CAP_STREAMING",
            "CAP_RESUMABLE",
            "CAP_SIGNED_URL",
            "CAP_MULTIPART",
            "CAP_INTROSPECTION",
            "CAP_DRY_RUN",
        ]:
            value = getattr(sdk, name)
            assert isinstance(value, str)
            assert value.startswith("supports_"), value

    def test_capability_dict_defaults_to_empty(self):
        # A connector that doesn't declare `capabilities` should be treated
        # as advertising nothing; the engine reads .get(flag, False).
        class Minimal(sdk.FileSource):
            name = "minimal"
            version = "0.0.0"

            def check(self):
                return sdk.CheckResult(ok=True)

            def list(self, prefix=None):
                return iter([])

            def open(self, path):
                return iter([])

        instance = Minimal(None)  # type: ignore[arg-type]
        assert type(instance).capabilities == {}
        assert type(instance).capabilities.get(sdk.CAP_STREAMING, False) is False


class TestVersioning:
    """`SDK_VERSION` and per-connector `min_sdk_version` form the
    contract-evolution mechanism. Engine refuses to load a connector
    requiring a newer SDK than is installed."""

    def test_sdk_version_is_a_string(self):
        assert isinstance(sdk.SDK_VERSION, str)
        major, _, _ = sdk.SDK_VERSION.partition(".")
        assert major.isdigit()

    def test_default_min_sdk_version_matches_sdk_version(self):
        # A connector that doesn't override `min_sdk_version` inherits
        # the SDK version it was compiled against; this is the right
        # default since the connector author tested against that version.
        class Defaulted(sdk.FileSource):
            name = "defaulted"
            version = "0.0.0"

            def check(self):
                return sdk.CheckResult(ok=True)

            def list(self, prefix=None):
                return iter([])

            def open(self, path):
                return iter([])

        assert Defaulted.min_sdk_version == sdk.SDK_VERSION


class TestConnectorConfig:
    """The Pydantic base provides validation + JSON Schema for free."""

    def test_subclass_validates_fields(self):
        from pydantic import Field, SecretStr, ValidationError

        class MyConfig(sdk.ConnectorConfig):
            bucket: str = Field(..., min_length=1)
            key: SecretStr

        # Happy path.
        cfg = MyConfig(bucket="b", key="hunter2")
        assert cfg.bucket == "b"

        # Required field missing.
        with pytest.raises(ValidationError):
            MyConfig(bucket="b")  # type: ignore[call-arg]

        # Forbidden extra field (model_config: extra='forbid').
        with pytest.raises(ValidationError):
            MyConfig(bucket="b", key="x", typo_field="oops")  # type: ignore[call-arg]

    def test_subclass_emits_json_schema(self):
        # HiFi auto-renders the config form from this schema; if it ever
        # stops emitting, every connector's UI form breaks.
        from pydantic import Field

        class MyConfig(sdk.ConnectorConfig):
            bucket: str = Field(..., description="S3 bucket name")
            region: str = "us-east-1"

        schema = MyConfig.model_json_schema()
        assert "properties" in schema
        assert "bucket" in schema["properties"]
        assert schema["properties"]["bucket"].get("description") == "S3 bucket name"

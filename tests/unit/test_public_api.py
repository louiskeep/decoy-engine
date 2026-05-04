"""
Tests for the public API surface declared in decoy_engine.__init__.

These tests guard the contract that CLI and platform code depend on. If
a name disappears from __all__ or its import path changes, that is a
breaking change and these tests should fail.
"""

import logging

import pytest

import decoy_engine
from decoy_engine import (
    Masker,
    DataGenerator,
    ExecutionContext,
    Logger,
    TelemetryClient,
    SchemaInspector,
    LicenseVerifier,
    DecoyError,
    ForgeError,
    ConfigError,
    PipelineValidationError,
    ConnectorError,
    ConnectorAuthError,
    LicenseError,
    LicenseExpiredError,
)


def test_all_lists_every_public_name():
    expected = {
        "Masker", "DataGenerator",
        "ExecutionContext", "Logger", "TelemetryClient",
        "SchemaInspector", "LicenseVerifier",
        "validate_config",
        "run_storm", "StormProfile", "FieldStats", "DetectorMatch", "SentinelFlag",
        "DecoyError", "ForgeError", "ConfigError", "PipelineValidationError",
        "ConnectorError", "ConnectorAuthError",
        "LicenseError", "LicenseExpiredError",
    }
    assert set(decoy_engine.__all__) == expected


def test_forge_error_is_deprecated_alias_for_decoy_error():
    assert ForgeError is DecoyError


def test_version_attribute_exists():
    assert isinstance(decoy_engine.__version__, str)


class TestExceptions:
    def test_config_error_subclasses_forge_error(self):
        assert issubclass(ConfigError, ForgeError)

    def test_pipeline_validation_error_subclasses_config_error(self):
        assert issubclass(PipelineValidationError, ConfigError)

    def test_connector_error_subclasses_forge_error(self):
        assert issubclass(ConnectorError, ForgeError)

    def test_connector_auth_error_subclasses_connector_error(self):
        assert issubclass(ConnectorAuthError, ConnectorError)

    def test_license_expired_error_subclasses_license_error(self):
        assert issubclass(LicenseExpiredError, LicenseError)


class TestLoggerProtocol:
    def test_stdlib_logger_satisfies_protocol(self):
        log = logging.getLogger("decoy_engine.test")
        assert isinstance(log, Logger)

    def test_object_missing_method_does_not_satisfy_protocol(self):
        class Incomplete:
            def info(self, msg): pass
            def warning(self, msg): pass
            def error(self, msg): pass
            # missing debug
        assert not isinstance(Incomplete(), Logger)


class TestExecutionContext:
    def test_default_construction(self):
        ctx = ExecutionContext()
        assert ctx.logger is None
        assert ctx.telemetry is None

    def test_logger_injection(self):
        log = logging.getLogger("decoy_engine.test")
        ctx = ExecutionContext(logger=log)
        assert ctx.logger is log


class TestStubs:
    def test_license_verifier_returns_free_tier(self):
        result = LicenseVerifier.verify()
        assert result["tier"] == "free"
        assert result["expires_at"] is None

    def test_schema_inspector_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            SchemaInspector()

# forge_engine/__init__.py
"""
forge_engine — data masking and synthetic generation library.

Public API (the contract CLI and platform code depend on):
    Masker            orchestrate a masking pipeline from a YAML config
    DataGenerator     generate synthetic data with referential integrity
    ExecutionContext  caller-provided runtime context (logger + telemetry)
    Logger            Protocol satisfied by stdlib loggers and Rich/DB-backed loggers
    TelemetryClient   Protocol for optional telemetry sinks
    SchemaInspector   connector schema introspection (stub, Phase 2)
    LicenseVerifier   license verification (stub)

Public exceptions (also in forge_engine.exceptions):
    ForgeError, ConfigError, PipelineValidationError,
    ConnectorError, ConnectorAuthError,
    LicenseError, LicenseExpiredError

Anything not listed in __all__ — and anything under forge_engine.internal —
is private and may change without a version bump.
"""

from forge_engine.masker import Masker
from forge_engine.generators import DataGenerator
from forge_engine.context import ExecutionContext, Logger, TelemetryClient
from forge_engine.schema import SchemaInspector
from forge_engine.license import LicenseVerifier
from forge_engine.exceptions import (
    ForgeError,
    ConfigError,
    PipelineValidationError,
    ConnectorError,
    ConnectorAuthError,
    LicenseError,
    LicenseExpiredError,
)

__version__ = '0.1.0'

__all__ = [
    'Masker',
    'DataGenerator',
    'ExecutionContext',
    'Logger',
    'TelemetryClient',
    'SchemaInspector',
    'LicenseVerifier',
    'ForgeError',
    'ConfigError',
    'PipelineValidationError',
    'ConnectorError',
    'ConnectorAuthError',
    'LicenseError',
    'LicenseExpiredError',
]

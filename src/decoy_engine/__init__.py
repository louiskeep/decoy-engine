# decoy_engine/__init__.py
"""
decoy_engine -- data masking and synthetic generation library.

Public API (the contract CLI and platform code depend on):
    Masker            orchestrate a masking pipeline from a YAML config
    DataGenerator     generate synthetic data with referential integrity
    ExecutionContext  caller-provided runtime context (logger + telemetry)
    Logger            Protocol satisfied by stdlib loggers and Rich/DB-backed loggers
    TelemetryClient   Protocol for optional telemetry sinks
    SchemaInspector   connector schema introspection (stub, Phase 2)
    LicenseVerifier   license verification (stub)

Public exceptions (also in decoy_engine.exceptions):
    DecoyError, ConfigError, PipelineValidationError,
    ConnectorError, ConnectorAuthError,
    LicenseError, LicenseExpiredError,
    FlagPauseSignal

`ForgeError` is a deprecated alias for `DecoyError` kept for one minor
version while the rebrand rolls through downstream consumers.

Anything not listed in __all__ -- and anything under decoy_engine.internal --
is private and may change without a version bump.
"""

from decoy_engine.masker import Masker
from decoy_engine.generators import DataGenerator
from decoy_engine.context import (
    ExecutionContext,
    Logger,
    StructuredEvents,
    TelemetryClient,
    emit_fidelity,
    emit_lineage,
    emit_quarantine,
    emit_step,
    emit_throughput_sample,
    make_key_resolver,
)
from decoy_engine.schema import SchemaInspector
from decoy_engine.license import LicenseVerifier
from decoy_engine.validation import validate_config
from decoy_engine.graph import (
    validate_graph,
    run_graph,
    preview_graph,
    RunResult,
    PreviewResult,
)
from decoy_engine.storm import run_storm, StormProfile, FieldStats, DetectorMatch, SentinelFlag
from decoy_engine.data_discovery import (
    DiscoveryResult,
    DiscoverySqlError,
    run_discovery_sql,
)
from decoy_engine.forecast import (
    recommend,
    ForecastReport,
    DisguiseRecommendation,
    FieldRecommendation,
    RiskFlag,
)
from decoy_engine.exceptions import (
    DecoyError,
    ForgeError,
    ConfigError,
    PipelineValidationError,
    ConnectorError,
    ConnectorAuthError,
    LicenseError,
    LicenseExpiredError,
    FlagPauseSignal,
)
from decoy_engine.internal.helpers import (
    register_faker_provider,
    unregister_faker_provider,
)

# Connector SDK.
from decoy_engine.sdk import (
    SDK_VERSION,
    CAP_STREAMING,
    CAP_RESUMABLE,
    CAP_SIGNED_URL,
    CAP_MULTIPART,
    CAP_INTROSPECTION,
    CAP_DRY_RUN,
    ConnectorConfig,
    FileMeta,
    CheckResult,
    WriteResult,
    FileSource,
    FileSink,
    TransientError,
    PermanentError,
)

__version__ = '0.1.0'

__all__ = [
    'Masker',
    'DataGenerator',
    'ExecutionContext',
    'Logger',
    'StructuredEvents',
    'TelemetryClient',
    'emit_step',
    'emit_lineage',
    'emit_fidelity',
    'emit_quarantine',
    'emit_throughput_sample',
    'make_key_resolver',
    'SchemaInspector',
    'LicenseVerifier',
    'validate_config',
    'validate_graph',
    'run_graph',
    'preview_graph',
    'RunResult',
    'PreviewResult',
    'run_storm',
    'StormProfile',
    'FieldStats',
    'DetectorMatch',
    'SentinelFlag',
    'run_discovery_sql',
    'DiscoveryResult',
    'DiscoverySqlError',
    'recommend',
    'ForecastReport',
    'DisguiseRecommendation',
    'FieldRecommendation',
    'RiskFlag',
    'DecoyError',
    'ForgeError',
    'ConfigError',
    'PipelineValidationError',
    'ConnectorError',
    'ConnectorAuthError',
    'LicenseError',
    'LicenseExpiredError',
    'FlagPauseSignal',
    'register_faker_provider',
    'unregister_faker_provider',
    # Connector SDK.
    'SDK_VERSION',
    'CAP_STREAMING',
    'CAP_RESUMABLE',
    'CAP_SIGNED_URL',
    'CAP_MULTIPART',
    'CAP_INTROSPECTION',
    'CAP_DRY_RUN',
    'ConnectorConfig',
    'FileMeta',
    'CheckResult',
    'WriteResult',
    'FileSource',
    'FileSink',
    'TransientError',
    'PermanentError',
]

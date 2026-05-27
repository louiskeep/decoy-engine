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

Public exceptions (also in decoy_engine.errors):
    DecoyError, ConfigError, PipelineValidationError,
    ConnectorError, ConnectorAuthError,
    LicenseError, LicenseExpiredError,
    FlagPauseSignal

Anything not listed in __all__ -- and anything under decoy_engine.internal --
is private and may change without a version bump.
"""

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
from decoy_engine.data_discovery import (
    DiscoveryResult,
    DiscoverySqlError,
    run_discovery_sql,
)
from decoy_engine.determinism import (
    SEED_PROTOCOL_VERSION,
    DeterminismError,
    Domain,
    IdentityDomain,
    derive,
    derive_index,
    derive_value,
)
from decoy_engine.errors import (
    ConfigError,
    ConnectorAuthError,
    ConnectorError,
    DecoyError,
    FlagPauseSignal,
    LicenseError,
    LicenseExpiredError,
    PipelineValidationError,
    ValidationError,
)
from decoy_engine.forecast import (
    DisguiseRecommendation,
    FieldRecommendation,
    ForecastReport,
    RiskFlag,
    recommend,
)
from decoy_engine.generators import DataGenerator
from decoy_engine.graph import (
    PreviewResult,
    RunResult,
    normalize_config,
    preview_graph,
    run_graph,
    validate_graph,
    validate_graph_full,
)
from decoy_engine.license import LicenseVerifier
from decoy_engine.masker import Masker
from decoy_engine.providers import (
    load_custom_providers,
    register_faker_list_provider,
    register_faker_provider,
    unregister_faker_provider,
)
from decoy_engine.relationships import (
    NamespaceBinding,
    NamespaceConfigError,
    NamespaceRegistry,
    OrphanPolicy,
    RelationshipEdge,
    RelationshipGraph,
    build_namespace_registry,
    build_relationship_graph,
    check_orphan_fk_policy_completeness,
)
from decoy_engine.schema import SchemaInspector

# Connector SDK.
from decoy_engine.sdk import (
    CAP_DRY_RUN,
    CAP_INTROSPECTION,
    CAP_MULTIPART,
    CAP_RESUMABLE,
    CAP_SIGNED_URL,
    CAP_STREAMING,
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
from decoy_engine.storm import DetectorMatch, FieldStats, SentinelFlag, StormProfile, run_storm
from decoy_engine.validation import validate_config
from decoy_engine.validation_result import (
    CODES as VALIDATION_CODES,
)
from decoy_engine.validation_result import (
    ValidationMessage,
    ValidationResult,
)

__version__ = "0.1.0"

__all__ = [
    "CAP_DRY_RUN",
    "CAP_INTROSPECTION",
    "CAP_MULTIPART",
    "CAP_RESUMABLE",
    "CAP_SIGNED_URL",
    "CAP_STREAMING",
    "SDK_VERSION",
    "SEED_PROTOCOL_VERSION",
    "VALIDATION_CODES",
    "CheckResult",
    "ConfigError",
    "ConnectorAuthError",
    "ConnectorConfig",
    "ConnectorError",
    "DataGenerator",
    "DecoyError",
    "DetectorMatch",
    "DeterminismError",
    "DiscoveryResult",
    "DiscoverySqlError",
    "DisguiseRecommendation",
    "Domain",
    "ExecutionContext",
    "FieldRecommendation",
    "FieldStats",
    "FileMeta",
    "FileSink",
    "FileSource",
    "FlagPauseSignal",
    "ForecastReport",
    "IdentityDomain",
    "LicenseError",
    "LicenseExpiredError",
    "LicenseVerifier",
    "Logger",
    "Masker",
    "NamespaceBinding",
    "NamespaceConfigError",
    "NamespaceRegistry",
    "OrphanPolicy",
    "PermanentError",
    "PipelineValidationError",
    "PreviewResult",
    "RelationshipEdge",
    "RelationshipGraph",
    "RiskFlag",
    "RunResult",
    "SchemaInspector",
    "SentinelFlag",
    "StormProfile",
    "StructuredEvents",
    "TelemetryClient",
    "TransientError",
    "ValidationError",
    "ValidationMessage",
    "ValidationResult",
    "WriteResult",
    "build_namespace_registry",
    "build_relationship_graph",
    "check_orphan_fk_policy_completeness",
    "derive",
    "derive_index",
    "derive_value",
    "emit_fidelity",
    "emit_lineage",
    "emit_quarantine",
    "emit_step",
    "emit_throughput_sample",
    "load_custom_providers",
    "make_key_resolver",
    "normalize_config",
    "preview_graph",
    "recommend",
    "register_faker_list_provider",
    "register_faker_provider",
    "run_discovery_sql",
    "run_graph",
    "run_storm",
    "unregister_faker_provider",
    "validate_config",
    "validate_graph",
    "validate_graph_full",
]

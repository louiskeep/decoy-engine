# decoy_engine/__init__.py
"""
decoy_engine -- data masking and synthetic generation library.

V1 public API (the contract CLI and platform code depend on today):
    Masker            orchestrate a masking pipeline from a YAML config
    DataGenerator     generate synthetic data with referential integrity
    ExecutionContext  caller-provided runtime context (logger + telemetry)
    Logger            Protocol satisfied by stdlib loggers and Rich/DB-backed loggers
    TelemetryClient   Protocol for optional telemetry sinks
    SchemaInspector   connector schema introspection (stub, Phase 2)
    LicenseVerifier   license verification (stub)

V2 plan/profile API (engine-v2 S1 deliverables; additive alongside V1):
    Plan              versioned plan artifact produced by compile_plan
    PlanCompileError  raised when a plan-compile check fails
    compile_plan      compile (config, profile, engine_version) -> Plan
    plan_from_yaml    deserialize a Plan from YAML
    plan_to_yaml      serialize a Plan to YAML
    Profile           frozen dataclass describing source data shape

    NOTE: profile_source (the function that produces a Profile by scanning
    a source file) is not yet exported. The scan-logic slice is pending.
    Import directly from decoy_engine.profile until it lands.

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

# V2 plan module (engine-v2 S1; additive alongside V1).
# profile_source is intentionally absent until the scan-logic slice lands.
from decoy_engine.plan import (
    Plan,
    PlanCompileError,
    compile_plan,
    plan_from_yaml,
    plan_to_yaml,
)
from decoy_engine.profile import Profile
from decoy_engine.providers import (
    load_custom_providers,
    register_faker_list_provider,
    register_faker_provider,
    unregister_faker_provider,
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
    # Connector SDK.
    "SDK_VERSION",
    "VALIDATION_CODES",
    "CheckResult",
    "ConfigError",
    "ConnectorAuthError",
    "ConnectorConfig",
    "ConnectorError",
    "DataGenerator",
    "DecoyError",
    "DetectorMatch",
    "DiscoveryResult",
    "DiscoverySqlError",
    "DisguiseRecommendation",
    "ExecutionContext",
    "FieldRecommendation",
    "FieldStats",
    "FileMeta",
    "FileSink",
    "FileSource",
    "FlagPauseSignal",
    "ForecastReport",
    "LicenseError",
    "LicenseExpiredError",
    "LicenseVerifier",
    "Logger",
    "Masker",
    # V2 plan/profile (engine-v2 S1; additive alongside V1).
    "Plan",
    "PlanCompileError",
    "PermanentError",
    "PipelineValidationError",
    "PreviewResult",
    "Profile",
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
    "compile_plan",
    "emit_fidelity",
    "emit_lineage",
    "emit_quarantine",
    "emit_step",
    "emit_throughput_sample",
    "load_custom_providers",
    "make_key_resolver",
    "normalize_config",
    "plan_from_yaml",
    "plan_to_yaml",
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

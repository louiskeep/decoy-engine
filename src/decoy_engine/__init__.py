# decoy_engine/__init__.py
"""
decoy_engine -- data masking and synthetic generation library.

Public API (the contract CLI and platform code depend on):
    PipelineConfig    strict pipeline-config schema; validate once at the choke-point
                      (PipelineConfig.model_validate(yaml).model_dump()) then hand the
                      dict to compile_plan / profile_source (decoy_engine.config)
    ExecutionContext  caller-provided runtime context (logger + telemetry)
    Logger            Protocol satisfied by stdlib loggers and Rich/DB-backed loggers
    TelemetryClient   Protocol for optional telemetry sinks
    SchemaInspector   connector schema introspection (stub, Phase 2)
    LicenseVerifier   license verification (stub)
    run_graph / preview_graph / validate_graph_full / normalize_config:
                      graph-mode entrypoints (the per-node graph still runs through
                      the v1 op surface; Masker / DataGenerator entry points were
                      removed in S9 as the platform mask + generate paths now go
                      through the v2 ExecutionAdapter directly).

Public exceptions (also in decoy_engine.errors):
    DecoyError, ConfigError, PipelineValidationError,
    ConnectorError, ConnectorAuthError,
    LicenseError, LicenseExpiredError,
    FlagPauseSignal

Anything not listed in __all__ -- and anything under decoy_engine.internal --
is private and may change without a version bump.

S9 NOTE: ``Masker`` and ``DataGenerator`` were removed from the public surface
in S9. The platform mask + generate paths now run exclusively through the v2
``ExecutionAdapter`` (mask) and ``generation.synthesize.generate_tables``
(generate) -- see ``api/jobs/v2_runner.py`` for the platform-side spine. The
underlying modules (``decoy_engine.masker``, ``decoy_engine.generators.generator``)
remain on disk for graph/op internal use but are no longer re-exported; any
external import of ``from decoy_engine import Masker`` / ``DataGenerator`` now
fails fast. This is the breaking public-API change ratified under the
``decoy_v2_clean_break`` PO directive.
"""

from decoy_engine.config import PipelineConfig
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
from decoy_engine.execution import (
    ExecutionAdapter,
    ExecutionError,
    ExecutionEvent,
    ExecutionResult,
    PandasExecutionAdapter,
    StrategyError,
    get_default_executor,
)
from decoy_engine.forecast import (
    DisguiseRecommendation,
    FieldRecommendation,
    ForecastReport,
    RiskFlag,
    recommend,
)
from decoy_engine.generation.composite import (
    BundlePool,
    CompositeAdapter,
    CompositeError,
    CompositeGenerator,
    composite_city_state_zip,
    composite_name_email,
)
from decoy_engine.generation.pool import (
    CardinalityMode,
    GenerationError,
    PoolAdapter,
    PoolBuilder,
    PoolCache,
    PoolCapacityError,
    PoolSampler,
    QualityWarning,
    ValuePool,
    get_default_pool_cache,
)
from decoy_engine.license import LicenseVerifier
from decoy_engine.plan.validate import (
    PlanCheckError,
    PlanValidationResult,
    validate_plan,
)
from decoy_engine.providers import (
    load_custom_providers,
    register_faker_list_provider,
    register_faker_provider,
    unregister_faker_provider,
)
from decoy_engine.providers_v2 import (
    AdapterError,
    BackendAdapter,
    CapabilityMatrix,
    ProviderError,
    ProviderRegistry,
    ProviderSpec,
    get_default_registry,
    register_faker_provider_v2,
)
from decoy_engine.providers_v2.identifiers import (
    EinAdapter,
    EinDomain,
    EinValidator,
    IdentifierError,
    IdentifierFormatError,
    MrnAdapter,
    MrnDomain,
    MrnValidator,
    NdcAdapter,
    NdcDomain,
    NdcValidator,
    NpiAdapter,
    NpiDomain,
    NpiValidator,
    SsnAdapter,
    SsnDomain,
    SsnValidator,
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
from decoy_engine.validation.post import (
    CompositeCoherenceReport,
    DistinctCount,
    FkValidityReport,
    NullCount,
    PostValidationRunner,
    QualitySummary,
)
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
    "AdapterError",
    "BackendAdapter",
    "BundlePool",
    "CapabilityMatrix",
    "CardinalityMode",
    "CheckResult",
    "CompositeAdapter",
    "CompositeCoherenceReport",
    "CompositeError",
    "CompositeGenerator",
    "ConfigError",
    "ConnectorAuthError",
    "ConnectorConfig",
    "ConnectorError",
    "DecoyError",
    "DetectorMatch",
    "DeterminismError",
    "DiscoveryResult",
    "DiscoverySqlError",
    "DisguiseRecommendation",
    "DistinctCount",
    "Domain",
    "EinAdapter",
    "EinDomain",
    "EinValidator",
    "ExecutionAdapter",
    "ExecutionContext",
    "ExecutionError",
    "ExecutionEvent",
    "ExecutionResult",
    "FieldRecommendation",
    "FieldStats",
    "FileMeta",
    "FileSink",
    "FileSource",
    "FkValidityReport",
    "FlagPauseSignal",
    "ForecastReport",
    "GenerationError",
    "IdentifierError",
    "IdentifierFormatError",
    "IdentityDomain",
    "LicenseError",
    "LicenseExpiredError",
    "LicenseVerifier",
    "Logger",
    "MrnAdapter",
    "MrnDomain",
    "MrnValidator",
    "NamespaceBinding",
    "NamespaceConfigError",
    "NamespaceRegistry",
    "NdcAdapter",
    "NdcDomain",
    "NdcValidator",
    "NpiAdapter",
    "NpiDomain",
    "NpiValidator",
    "NullCount",
    "OrphanPolicy",
    "PandasExecutionAdapter",
    "PermanentError",
    "PipelineConfig",
    "PipelineValidationError",
    "PlanCheckError",
    "PlanValidationResult",
    "PoolAdapter",
    "PoolBuilder",
    "PoolCache",
    "PoolCapacityError",
    "PoolSampler",
    "PostValidationRunner",
    "ProviderError",
    "ProviderRegistry",
    "ProviderSpec",
    "QualitySummary",
    "QualityWarning",
    "RelationshipEdge",
    "RelationshipGraph",
    "RiskFlag",
    "SchemaInspector",
    "SentinelFlag",
    "SsnAdapter",
    "SsnDomain",
    "SsnValidator",
    "StormProfile",
    "StrategyError",
    "StructuredEvents",
    "TelemetryClient",
    "TransientError",
    "ValidationError",
    "ValidationMessage",
    "ValidationResult",
    "ValuePool",
    "WriteResult",
    "build_namespace_registry",
    "build_relationship_graph",
    "check_orphan_fk_policy_completeness",
    "composite_city_state_zip",
    "composite_name_email",
    "derive",
    "derive_index",
    "derive_value",
    "emit_fidelity",
    "emit_lineage",
    "emit_quarantine",
    "emit_step",
    "emit_throughput_sample",
    "get_default_executor",
    "get_default_pool_cache",
    "get_default_registry",
    "load_custom_providers",
    "make_key_resolver",
    "recommend",
    "register_faker_list_provider",
    "register_faker_provider",
    "register_faker_provider_v2",
    "run_discovery_sql",
    "run_storm",
    "unregister_faker_provider",
    "validate_config",
    "validate_plan",
]

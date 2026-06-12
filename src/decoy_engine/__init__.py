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
    compile_plan      compile a validated config + Profile into a frozen Plan
                      (decoy_engine.plan)
    ExecutionAdapter / PandasExecutionAdapter / PolarsExecutionAdapter /
    select_execution_adapter / get_default_executor:
                      the plan-to-data execution boundary; the Polars adapter is
                      the default substrate. The caller runs
                      `select_execution_adapter().run(plan, source) -> ExecutionResult`.
    generate_tables   (decoy_engine.generation.synthesize) table-from-schema
                      synthesis for generate-mode configs.

Public exceptions (also in decoy_engine.errors):
    DecoyError, ConfigError, PipelineValidationError,
    ConnectorError, ConnectorAuthError,
    LicenseError, LicenseExpiredError,
    FlagPauseSignal

Anything not listed in __all__ -- and anything under decoy_engine.internal --
is private and may change without a version bump.

V2 NOTE: the V1 public surface (``Masker``, ``DataGenerator``, ``run_graph`` /
``preview_graph`` / ``validate_graph*``) and the V1 graph runner were removed
under the ``decoy_v2_clean_break`` PO directive (the final V1 graph-runner and
V1-only transform deletion landed in S22, 2026-05-30). The engine is now
plan-first: a caller validates a ``PipelineConfig`` at the choke-point, profiles
the source, calls ``compile_plan`` to produce a frozen ``Plan``, and hands the
plan to an ``ExecutionAdapter`` (Polars by default) for a MASK job, or to
``generation.synthesize.generate_tables`` for a GENERATE job. See
``api/jobs/v2_runner.py`` for the platform-side spine and
``tests/integration/golden/test_execution_e2e.py`` for the canonical end-to-end
call shape. Any external import of ``Masker`` / ``DataGenerator`` / ``run_graph``
now fails fast.
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
    classify_table_kinds,
    get_default_executor,
    run_pipeline,
    select_execution_adapter,
)
from decoy_engine.generation.composite import (
    BundlePool,
    CompositeAdapter,
    CompositeAddress,
    CompositeCustom,
    CompositeError,
    CompositeGenerator,
    CompositePerson,
    CompositeProvider,
    composite_address,
    composite_city_state_zip,
    composite_custom,
    composite_name_email,
    composite_person,
    composite_provider,
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
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.license import LicenseVerifier
from decoy_engine.plan import compile_plan, run_config_only_checks
from decoy_engine.plan.validate import (
    PlanCheckError,
    PlanValidationResult,
    validate_plan,
)
from decoy_engine.providers import (
    atomic_swap_db_providers,
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
from decoy_engine.unmask import UnmaskColumnReport, UnmaskResult, unmask_pipeline
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
    "CompositeAddress",
    "CompositeCoherenceReport",
    "CompositeCustom",
    "CompositeError",
    "CompositeGenerator",
    "CompositePerson",
    "CompositeProvider",
    "ConfigError",
    "ConnectorAuthError",
    "ConnectorConfig",
    "ConnectorError",
    "DecoyError",
    "DetectorMatch",
    "DeterminismError",
    "DiscoveryResult",
    "DiscoverySqlError",
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
    "FieldStats",
    "FileMeta",
    "FileSink",
    "FileSource",
    "FkValidityReport",
    "FlagPauseSignal",
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
    "UnmaskColumnReport",
    "UnmaskResult",
    "ValidationError",
    "ValidationMessage",
    "ValidationResult",
    "ValuePool",
    "WriteResult",
    "atomic_swap_db_providers",
    "build_namespace_registry",
    "build_relationship_graph",
    "check_orphan_fk_policy_completeness",
    "classify_table_kinds",
    "compile_plan",
    "composite_address",
    "composite_city_state_zip",
    "composite_custom",
    "composite_name_email",
    "composite_person",
    "composite_provider",
    "derive",
    "derive_index",
    "derive_value",
    "emit_fidelity",
    "emit_lineage",
    "emit_quarantine",
    "emit_step",
    "emit_throughput_sample",
    "generate_tables",
    "get_default_executor",
    "get_default_pool_cache",
    "get_default_registry",
    "load_custom_providers",
    "make_key_resolver",
    "register_faker_list_provider",
    "register_faker_provider",
    "register_faker_provider_v2",
    "run_config_only_checks",
    "run_discovery_sql",
    "run_pipeline",
    "run_storm",
    "select_execution_adapter",
    "unmask_pipeline",
    "unregister_faker_provider",
    "validate_config",
    "validate_plan",
]

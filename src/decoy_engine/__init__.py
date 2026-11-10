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
    compile_plan      compile a validated config + Profile into a frozen Plan
                      (decoy_engine.plan)
    ExecutionAdapter / PandasExecutionAdapter /
    select_execution_adapter / get_default_executor:
                      the plan-to-data execution boundary; the pandas adapter is
                      the parity oracle (native kernels are route-chosen). The caller runs
                      `select_execution_adapter().run(plan, source) -> ExecutionResult`.
    generate_tables   (decoy_engine.generation.synthesize) table-from-schema
                      synthesis for generate-mode configs.
    generated_table_to_pandas  (decoy_engine.generation) Arrow table to pandas,
                      nullable integers stay integer (a CSV prints `1`, not `1.0`).
    read_fixed_width  (decoy_engine.profile._fixed_width_reader) parse a fixed-width
                      file into a DataFrame per a FixedWidthLayout or `FileSource.layout`
                      dict; raises FixedWidthParseError or ConfigError.
    read_csv_source / CsvDialect / resolve_csv_dialect:
                      (decoy_engine.profile._csv_reader) the one CSV source
                      reader: a file read under its declared dialect (header or
                      `column_names`, delimiter), with a fail-closed width scan
                      for headerless files. `resolve_csv_dialect(descriptor)`
                      turns a source descriptor into the `CsvDialect`.
    RELEASE_PHASE / ReleasePhase / is_pre_ga:
                      (decoy_engine.release) the single pre-GA/GA switch the rule
                      inversions and CI gates read. Pre-GA: hard-delete is allowed
                      and the compatibility contract is advisory. The platform reads
                      RELEASE_PHASE in process (no separate platform constant).

Public exceptions (also in decoy_engine.errors):
    DecoyError, ConfigError, PipelineValidationError,
    ConnectorError, ConnectorAuthError,
    LicenseError, LicenseExpiredError,
    FlagPauseSignal, FixedWidthParseError

Anything not in __all__, and anything under decoy_engine.internal, is private.

V2 NOTE: the V1 surface (``Masker``, ``DataGenerator``, ``run_graph``) was removed
in S22 (2026-05-30). The engine is plan-first: validate a ``PipelineConfig``, profile
the source, ``compile_plan``, then run the plan through an ``ExecutionAdapter`` (MASK)
or ``generation.synthesize.generate_tables`` (GENERATE). See
``tests/integration/golden/test_execution_e2e.py`` for the end-to-end call shape.
"""

import logging

# ruff: noqa: E402  (the NullHandler attach must precede every submodule import)
# The one NullHandler attach site; `_configure_logger` preserves it and never creates one.
logging.getLogger(__name__).addHandler(logging.NullHandler())

from decoy_engine.config import (
    FILE_SOURCE_PATH_FIELDS,
    Dismissal,
    PipelineConfig,
    PreScanConfig,
    parse_pipeline_config,
)
from decoy_engine.config._pipeline import is_valid_pipeline_name
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
from decoy_engine.detection import (
    DetectorMatch,
    ModelStatus,
    ModelUnavailableError,
    ner_status,
)
from decoy_engine.detection.types import ModelRef
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
    ConfigRuleError,
    ConnectorAuthError,
    ConnectorError,
    DecoyError,
    FixedWidthParseError,
    FlagPauseSignal,
    LicenseError,
    LicenseExpiredError,
    PipelineValidationError,
    ValidationError,
)
from decoy_engine.execution import (
    CapacityEstimate,
    CapacityVerdict,
    ExecutionAdapter,
    ExecutionError,
    ExecutionEvent,
    ExecutionResult,
    JobEstimate,
    PandasExecutionAdapter,
    StrategyError,
    classify_table_kinds,
    estimate_job,
    estimate_job_capacity,
    get_default_executor,
    run_pipeline,
    select_execution_adapter,
)
from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._strategies import StrategyInfo, StrategyOption, strategy_catalog
from decoy_engine.execution._transforms_table import apply_table_transforms
from decoy_engine.execution.native._chunked_entry import run_mask_chunked
from decoy_engine.execution.native._companion_status import (
    NativeCompanionStatus,
    native_companion_status,
)
from decoy_engine.execution.out_of_core import resolve_conservative_ooc_budget_bytes
from decoy_engine.execution.pipeline import detect_usable_cpus
from decoy_engine.fileaccess import open_protected_dir, protected_scratch_dir
from decoy_engine.generation import generated_table_to_pandas
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
from decoy_engine.keyprovider import (
    KeyedStrategyRequiresSecret,
    KeyProvider,
    MaskSecretError,
    MissingMaskSecret,
    SecretKeyProvider,
    SeedKeyProvider,
    WeakMaskSecret,
)
from decoy_engine.plan import (
    compile_plan,
    job_seed_for_config,
    pipeline_config_hash,
    run_config_only_checks,
)
from decoy_engine.plan.validate import (
    PlanCheckError,
    PlanValidationResult,
    validate_plan,
)
from decoy_engine.profile._csv_reader import CsvDialect, read_csv_source, resolve_csv_dialect
from decoy_engine.profile._declared_columns import (
    DeclarationRequest,
    PreparedDeclaredSources,
    check_columns_declared,
    declared_columns,
    prepare_declared_sources,
    source_columns,
)
from decoy_engine.profile._fixed_width_reader import read_fixed_width
from decoy_engine.profile._fixed_width_writer import write_fixed_width
from decoy_engine.profiling import (
    ColumnProfile,
    FieldStats,
    ScanSet,
    ScanSetCore,
    SentinelFlag,
    TableScan,
    distribution_snapshot_document,
    is_value_sensitive,
    profile_columns,
    profile_document,
    redact_profile_dict,
    scan_set_to_dict,
    scan_sources,
    snapshot_joint_warnings,
)
from decoy_engine.profiling.model_pack import classifier_status, load_detectors
from decoy_engine.progress import (
    CallbackSink,
    CheckpointEvent,
    CheckpointInfo,
    PhaseEvent,
    ProgressSink,
    PublicationInfo,
    RateSample,
    RetryEvent,
    RetryInfo,
    RouteEvent,
    StructuredEventsSink,
    TerminalEvent,
    WarningEvent,
    event_to_dict,
    event_to_legacy_dict,
)
from decoy_engine.providers import (
    atomic_swap_db_providers,
    list_generate_faker_providers,
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

# F9: these identifier families are no longer in decoy_engine.__all__ (use the
# decoy_engine.identifiers sub-import). The bindings are kept here for
# backward-compatible attribute access; see _F9_KEPT_BINDINGS below.
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
from decoy_engine.reason_adapter import reason_from_exception, reasons_from_report
from decoy_engine.reasons import ReasonContext, ReasonRecord
from decoy_engine.records import DecodedRecord, OpaqueRecord, RecordError, RecordNeedsNewer
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
from decoy_engine.release import RELEASE_PHASE, ReleasePhase, is_pre_ga
from decoy_engine.run_result import RUN_RESULT_SCHEMA, RunResult, RunResultRead, derive_outcome
from decoy_engine.safe_output import ConfigProblem

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
from decoy_engine.secret_providers import (
    KeyedColumn,
    SecretCachePolicy,
    SecretDescriptor,
    SecretProvider,
    SecretProviderError,
    SecretProviderRegistry,
    SecretRef,
    keyed_columns_for_config,
    register_secret_provider,
)
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
from decoy_engine.vault import VaultError, VaultWriter, load_vault, vault_writer_for_config

# F9: names trimmed from __all__ but kept as module bindings so attribute access
# (decoy_engine.EinValidator, ...) stays non-breaking; anchoring them here marks the
# imports as intentional re-exports. Canonical home: decoy_engine.identifiers.
_F9_KEPT_BINDINGS = (
    BundlePool,
    CompositeAddress,
    PoolCache,
    composite_city_state_zip,
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

__version__ = "0.19.0"

__all__ = [
    "CAP_DRY_RUN",
    "CAP_INTROSPECTION",
    "CAP_MULTIPART",
    "CAP_RESUMABLE",
    "CAP_SIGNED_URL",
    "CAP_STREAMING",
    "FILE_SOURCE_PATH_FIELDS",
    "RELEASE_PHASE",
    "RUN_RESULT_SCHEMA",
    "SDK_VERSION",
    "SEED_PROTOCOL_VERSION",
    "VALIDATION_CODES",
    "AdapterError",
    "BackendAdapter",
    "CallbackSink",
    "CapabilityMatrix",
    "CapacityEstimate",
    "CapacityVerdict",
    "CardinalityMode",
    "CheckResult",
    "CheckpointEvent",
    "CheckpointInfo",
    "ColumnProfile",
    "CompositeAdapter",
    "CompositeCoherenceReport",
    "CompositeCustom",
    "CompositeError",
    "CompositeGenerator",
    "CompositePerson",
    "CompositeProvider",
    "ConfigError",
    "ConfigProblem",
    "ConfigRuleError",
    "ConnectorAuthError",
    "ConnectorConfig",
    "ConnectorError",
    "CsvDialect",
    "DeclarationRequest",
    "DecodedRecord",
    "DecoyError",
    "DetectorMatch",
    "DeterminismError",
    "DiscoveryResult",
    "DiscoverySqlError",
    "Dismissal",
    "DistinctCount",
    "Domain",
    "ExecutionAdapter",
    "ExecutionContext",
    "ExecutionError",
    "ExecutionEvent",
    "ExecutionResult",
    "FieldStats",
    "FileMeta",
    "FileSink",
    "FileSource",
    "FixedWidthParseError",
    "FkValidityReport",
    "FlagPauseSignal",
    "GenerationError",
    "IdentityDomain",
    "JobEstimate",
    "KeyProvider",
    "KeyedColumn",
    "KeyedStrategyRequiresSecret",
    "LicenseError",
    "LicenseExpiredError",
    "Logger",
    "MaskSecretError",
    "MissingMaskSecret",
    "ModelRef",
    "ModelStatus",
    "ModelUnavailableError",
    "NamespaceBinding",
    "NamespaceConfigError",
    "NamespaceRegistry",
    "NativeCompanionStatus",
    "NullCount",
    "OpaqueRecord",
    "OrphanPolicy",
    "PandasExecutionAdapter",
    "PermanentError",
    "PhaseEvent",
    "PipelineConfig",
    "PipelineValidationError",
    "PlanCheckError",
    "PlanValidationResult",
    "PoolAdapter",
    "PoolBuilder",
    "PoolCapacityError",
    "PoolSampler",
    "PostValidationRunner",
    "PreScanConfig",
    "PreparedDeclaredSources",
    "ProgressSink",
    "ProviderError",
    "ProviderRegistry",
    "ProviderSpec",
    "PublicationInfo",
    "QualitySummary",
    "QualityWarning",
    "RateSample",
    "ReasonContext",
    "ReasonRecord",
    "RecordError",
    "RecordNeedsNewer",
    "RelationshipEdge",
    "RelationshipGraph",
    "ReleasePhase",
    "RetryEvent",
    "RetryInfo",
    "RouteEvent",
    "RunResult",
    "RunResultRead",
    "ScanSet",
    "ScanSetCore",
    "SecretCachePolicy",
    "SecretDescriptor",
    "SecretKeyProvider",
    "SecretProvider",
    "SecretProviderError",
    "SecretProviderRegistry",
    "SecretRef",
    "SeedKeyProvider",
    "SentinelFlag",
    "StrategyError",
    "StrategyInfo",
    "StrategyOption",
    "StructuredEvents",
    "StructuredEventsSink",
    "TableScan",
    "TelemetryClient",
    "TerminalEvent",
    "TransientError",
    "UnmaskColumnReport",
    "UnmaskResult",
    "ValidationError",
    "ValidationMessage",
    "ValidationResult",
    "ValuePool",
    "VaultError",
    "VaultWriter",
    "WarningEvent",
    "WeakMaskSecret",
    "WriteResult",
    "apply_table_transforms",
    "atomic_swap_db_providers",
    "build_namespace_registry",
    "build_relationship_graph",
    "check_columns_declared",
    "check_orphan_fk_policy_completeness",
    "classifier_status",
    "classify_table_kinds",
    "compile_plan",
    "composite_address",
    "composite_custom",
    "composite_name_email",
    "composite_person",
    "composite_provider",
    "declared_columns",
    "derive",
    "derive_index",
    "derive_outcome",
    "derive_value",
    "detect_usable_cpus",
    "distribution_snapshot_document",
    "emit_fidelity",
    "emit_lineage",
    "emit_quarantine",
    "emit_step",
    "emit_throughput_sample",
    "estimate_job",
    "estimate_job_capacity",
    "event_to_dict",
    "event_to_legacy_dict",
    "generate_tables",
    "generated_table_to_pandas",
    "get_default_executor",
    "get_default_pool_cache",
    "get_default_registry",
    "is_pre_ga",
    "is_valid_pipeline_name",
    "is_value_sensitive",
    "job_seed_for_config",
    "keyed_columns_for_config",
    "list_generate_faker_providers",
    "load_custom_providers",
    "load_detectors",
    "load_vault",
    "make_key_resolver",
    "native_companion_status",
    "ner_status",
    "open_protected_dir",
    "parse_pipeline_config",
    "pipeline_config_hash",
    "prepare_declared_sources",
    "profile_columns",
    "profile_document",
    "protected_scratch_dir",
    "read_csv_source",
    "read_fixed_width",
    "reason_from_exception",
    "reasons_from_report",
    "redact_profile_dict",
    "register_faker_list_provider",
    "register_faker_provider",
    "register_faker_provider_v2",
    "register_secret_provider",
    "resolve_conservative_ooc_budget_bytes",
    "resolve_csv_dialect",
    "run_config_only_checks",
    "run_discovery_sql",
    "run_mask_chunked",
    "run_mask_pipeline_chunked",
    "run_pipeline",
    "scan_set_to_dict",
    "scan_sources",
    "select_execution_adapter",
    "snapshot_joint_warnings",
    "source_columns",
    "strategy_catalog",
    "unmask_pipeline",
    "unregister_faker_provider",
    "validate_config",
    "validate_plan",
    "vault_writer_for_config",
    "write_fixed_width",
]

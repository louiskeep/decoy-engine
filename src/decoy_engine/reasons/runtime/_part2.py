"""Catalog of the ``runtime`` namespace, part 2."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Outcome, Phase, RetryClass

__all__ = [
    "BAD_SECRET_REF",
    "BUCKETIZE_WIDTH_UNRESOLVABLE",
    "BUCKET_PERTURB_INVALID_CONFIG",
    "BUCKET_PERTURB_REQUIRES_NAMESPACE",
    "BUCKET_PERTURB_UNRECOGNIZED_BUCKET",
    "BUNDLE_MISSING_OUTPUT_COLUMNS",
    "CARDINALITY_TARGET_EXCEEDS_POOL",
    "CATEGORICAL_CATEGORIES_NOT_LIST",
    "DETERMINISTIC_MODE_UNSUPPORTED_CARDINALITY",
    "DETERMINISTIC_REQUIRES_SOURCE_AND_NAMESPACE",
    "EXACT_INT_NULL_MASK_MISMATCH",
    "FLOAT_CANONICALIZATION_UNSUPPORTED",
    "FPE_CHECKSUM_UNSUPPORTED",
    "FPE_CONFIG_INVALID",
    "FPE_UNENCRYPTABLE",
    "FPE__CHECKSUM_INVALID_SOURCE",
    "FPE__UNENCRYPTABLE_DOMAIN",
    "FPE__UNENCRYPTABLE_LENGTH",
    "GENERATION_BUDGET_EXCEEDED",
    "GENERATION_COLUMN_UNBOUNDED",
    "GENERATION_NOT_CLASS_A",
    "GENERATION_SHAPE_UNSUPPORTED",
    "GENERATION_TABLE_UNKNOWN",
    "GENERATION_WINDOW_INVALID",
    "GENERATION_WINDOW_TIMEOUT",
    "INDEX_BATCH_LENGTH_MISMATCH",
    "INDEX_BATCH_NULL_MASK_MISMATCH",
    "INDEX_BATCH_OUT_OF_BOUNDS",
    "INDEX_BATCH_TYPE_MISMATCH",
    "INVALID_SEED_LENGTH",
    "KEYED_STRATEGY_REQUIRES_SECRET",
    "MASK_KEY_DERIVATION_FAILED",
    "MASK_KEY_REQUIRED",
    "MISSING_MASK_SECRET",
    "MIXED_DRIVER_UNSUPPORTED",
    "MIXED_FK_CROSS_GENERATE_UNSUPPORTED",
    "MIXED_FK_TOPOLOGY_UNSUPPORTED",
    "NATIVE_CHUNK_SCHEMA_DRIFT",
    "NATIVE_COMPANION_CHECK_FAILED",
    "NATIVE_TYPE_NOT_ADMITTED",
    "NULL_CANONICALIZATION_UNREACHABLE",
    "OUT_OF_CORE_REORDER_THRESHOLD_INVALID",
    "OUT_OF_CORE_REORDER_UNBUDGETED",
    "OUT_OF_CORE_RESERVED_BYTES_INVALID",
    "OUT_OF_CORE_SORTER_SCHEMA_NOT_SLIM",
    "OUT_OF_CORE_SORT_INVALID_STATE",
    "OUT_OF_CORE_SORT_KEY_MISSING",
    "OUT_OF_CORE_SORT_KEY_NULL",
    "OUT_OF_CORE_SORT_KEY_TYPE_DRIFT",
    "OUT_OF_CORE_SORT_KEY_TYPE_UNSUPPORTED",
    "OUT_OF_CORE_SORT_ROW_INDEX_UNBOUNDED",
    "OUT_OF_CORE_SORT_ROW_TOO_WIDE",
    "OUT_OF_CORE_SOURCE_MISSING",
    "OUT_OF_CORE_SOURCE_SCHEMA_REQUIRED",
    "OUT_OF_CORE_STRATEGY_UNSUPPORTED",
    "OUT_OF_CORE_TEMP_DISK_EXCEEDED",
    "OUT_OF_CORE_TEXT_MASK_COLUMN_MISSING",
    "OUT_OF_CORE_TEXT_MASK_NATIVE_RESULT_INVALID",
    "OUT_OF_CORE_TEXT_MASK_SOURCE_TYPE_DRIFT",
    "OUT_OF_CORE_TEXT_MASK_UNPREPARED",
    "OUT_OF_CORE_TEXT_REDACT_COLUMN_MISSING",
    "OUT_OF_CORE_TEXT_REDACT_NATIVE_RESULT_INVALID",
    "OUT_OF_CORE_TEXT_REDACT_SOURCE_TYPE_DRIFT",
    "OUT_OF_CORE_TEXT_REDACT_UNPREPARED",
    "OUT_OF_CORE_VALIDATORS_NOT_ADMITTED",
    "OUT_OF_CORE_VAULT_CONFIG_REQUIRED",
    "PK_DUPLICATES",
    "POOL_CAPACITY_UNVERIFIABLE_NO_PROFILE",
    "POOL_EXCEEDS_CACHE_BUDGET",
    "POOL_QUALITY_THRESHOLD_EXCEEDED",
    "POOL_TOO_SMALL_FOR_SOURCE",
    "PROVIDER_NOT_POOLABLE",
    "RAW_HEX_BATCH_LENGTH_MISMATCH",
    "RAW_HEX_BATCH_TYPE_MISMATCH",
    "RAW_HEX_BATCH_UNEXPECTED_NULL",
    "RELATIONSHIP_CYCLE",
    "ROW_EXCEEDS_MAX_ROW_BYTES",
    "RUN_SINGLE_REQUIRES_TABLE",
    "SEQUENTIAL_BUCKET_PERTURB_INPUT_TYPE_DRIFT",
    "SEQUENTIAL_CATEGORICAL_INPUT_TYPE_DRIFT",
    "SEQUENTIAL_COMPOSITE_FK_CHAIN_UNSUPPORTED",
    "SEQUENTIAL_DATE_SHIFT_INPUT_TYPE_DRIFT",
    "SEQUENTIAL_FAKER_INPUT_TYPE_DRIFT",
    "SEQUENTIAL_FPE_INPUT_TYPE_DRIFT",
    "SEQUENTIAL_FPE_KEY_INTEGER_NOT_PRESERVED",
    "SEQUENTIAL_FPE_NATIVE_RESULT_INVALID",
    "SEQUENTIAL_GROUP_KEY_INPUT_TYPE_DRIFT",
    "SEQUENTIAL_HASH_INPUT_TYPE_DRIFT",
    "SEQUENTIAL_TEXT_MASK_INPUT_TYPE_DRIFT",
    "SEQUENTIAL_TEXT_REDACT_INPUT_TYPE_DRIFT",
    "SOURCE_LENGTH_MISMATCH",
    "SOURCE_REQUIRED_FOR_CARDINALITY_MODE",
    "SPLIT_SINK_NEEDS_ALL_DISPATCHED",
    "TIMEZONE_NAIVE_DATETIME",
    "TRANSFORM_SOURCE_MISSING",
    "UNDECLARED_OUTPUT_COLUMNS",
    "UNIQUENESS_IMPOSSIBLE",
    "UNKNOWN_CARDINALITY_MODE",
    "UNSUPPORTED_STRATEGY",
    "VALUE_EXCEEDS_DECLARED_BOUND",
]

_d = _catalog.runtime("ExecutionError")

OUT_OF_CORE_REORDER_THRESHOLD_INVALID = _d(
    "out_of_core_reorder_threshold_invalid", "Out of core reorder threshold invalid."
)
OUT_OF_CORE_REORDER_UNBUDGETED = _d(
    "out_of_core_reorder_unbudgeted", "Out of core reorder unbudgeted."
)
OUT_OF_CORE_RESERVED_BYTES_INVALID = _d(
    "out_of_core_reserved_bytes_invalid", "Out of core reserved bytes invalid."
)
OUT_OF_CORE_SORT_INVALID_STATE = _d(
    "out_of_core_sort_invalid_state", "Out of core sort invalid state."
)
OUT_OF_CORE_SORT_KEY_MISSING = _d("out_of_core_sort_key_missing", "Out of core sort key missing.")
OUT_OF_CORE_SORT_KEY_NULL = _d("out_of_core_sort_key_null", "Out of core sort key null.")
OUT_OF_CORE_SORT_KEY_TYPE_DRIFT = _d(
    "out_of_core_sort_key_type_drift", "Out of core sort key type drift."
)
OUT_OF_CORE_SORT_KEY_TYPE_UNSUPPORTED = _d(
    "out_of_core_sort_key_type_unsupported", "Out of core sort key type unsupported."
)
OUT_OF_CORE_SORT_ROW_INDEX_UNBOUNDED = _d(
    "out_of_core_sort_row_index_unbounded", "Out of core sort row index unbounded."
)
OUT_OF_CORE_SORT_ROW_TOO_WIDE = _d(
    "out_of_core_sort_row_too_wide", "Out of core sort row too wide."
)
OUT_OF_CORE_SORTER_SCHEMA_NOT_SLIM = _d(
    "out_of_core_sorter_schema_not_slim", "Out of core sorter schema not slim."
)
OUT_OF_CORE_SOURCE_MISSING = _d("out_of_core_source_missing", "Out of core source missing.")
OUT_OF_CORE_SOURCE_SCHEMA_REQUIRED = _d(
    "out_of_core_source_schema_required", "Out of core source schema required."
)
OUT_OF_CORE_STRATEGY_UNSUPPORTED = _d(
    "out_of_core_strategy_unsupported", "Out of core strategy unsupported."
)
OUT_OF_CORE_TEMP_DISK_EXCEEDED = _d(
    "out_of_core_temp_disk_exceeded", "Out of core temp disk exceeded."
)
OUT_OF_CORE_TEXT_MASK_COLUMN_MISSING = _d(
    "out_of_core_text_mask_column_missing", "Out of core text mask column missing."
)
OUT_OF_CORE_TEXT_MASK_NATIVE_RESULT_INVALID = _d(
    "out_of_core_text_mask_native_result_invalid", "Out of core text mask native result invalid."
)
OUT_OF_CORE_TEXT_MASK_SOURCE_TYPE_DRIFT = _d(
    "out_of_core_text_mask_source_type_drift", "Out of core text mask source type drift."
)
OUT_OF_CORE_TEXT_MASK_UNPREPARED = _d(
    "out_of_core_text_mask_unprepared", "Out of core text mask unprepared."
)
OUT_OF_CORE_TEXT_REDACT_COLUMN_MISSING = _d(
    "out_of_core_text_redact_column_missing", "Out of core text redact column missing."
)
OUT_OF_CORE_TEXT_REDACT_NATIVE_RESULT_INVALID = _d(
    "out_of_core_text_redact_native_result_invalid",
    "Out of core text redact native result invalid.",
)
OUT_OF_CORE_TEXT_REDACT_SOURCE_TYPE_DRIFT = _d(
    "out_of_core_text_redact_source_type_drift", "Out of core text redact source type drift."
)
OUT_OF_CORE_TEXT_REDACT_UNPREPARED = _d(
    "out_of_core_text_redact_unprepared", "Out of core text redact unprepared."
)
OUT_OF_CORE_VALIDATORS_NOT_ADMITTED = _d(
    "out_of_core_validators_not_admitted", "Out of core validators not admitted."
)
OUT_OF_CORE_VAULT_CONFIG_REQUIRED = _d(
    "out_of_core_vault_config_required", "Out of core vault config required."
)
RELATIONSHIP_CYCLE = _d("relationship_cycle", "Relationship cycle.")
RUN_SINGLE_REQUIRES_TABLE = _d("run_single_requires_table", "Run single requires table.")
SEQUENTIAL_BUCKET_PERTURB_INPUT_TYPE_DRIFT = _d(
    "sequential_bucket_perturb_input_type_drift", "Sequential bucket perturb input type drift."
)
SEQUENTIAL_CATEGORICAL_INPUT_TYPE_DRIFT = _d(
    "sequential_categorical_input_type_drift", "Sequential categorical input type drift."
)
SEQUENTIAL_COMPOSITE_FK_CHAIN_UNSUPPORTED = _d(
    "sequential_composite_fk_chain_unsupported", "Sequential composite FK chain unsupported."
)
SEQUENTIAL_DATE_SHIFT_INPUT_TYPE_DRIFT = _d(
    "sequential_date_shift_input_type_drift", "Sequential date shift input type drift."
)
SEQUENTIAL_FAKER_INPUT_TYPE_DRIFT = _d(
    "sequential_faker_input_type_drift", "Sequential Faker input type drift."
)
SEQUENTIAL_FPE_INPUT_TYPE_DRIFT = _d(
    "sequential_fpe_input_type_drift", "Sequential FPE input type drift."
)
SEQUENTIAL_FPE_KEY_INTEGER_NOT_PRESERVED = _d(
    "sequential_fpe_key_integer_not_preserved", "Sequential FPE key integer not preserved."
)
SEQUENTIAL_FPE_NATIVE_RESULT_INVALID = _d(
    "sequential_fpe_native_result_invalid", "Sequential FPE native result invalid."
)
SEQUENTIAL_GROUP_KEY_INPUT_TYPE_DRIFT = _d(
    "sequential_group_key_input_type_drift", "Sequential group key input type drift."
)
SEQUENTIAL_HASH_INPUT_TYPE_DRIFT = _d(
    "sequential_hash_input_type_drift", "Sequential hash input type drift."
)
SEQUENTIAL_TEXT_MASK_INPUT_TYPE_DRIFT = _d(
    "sequential_text_mask_input_type_drift", "Sequential text mask input type drift."
)
SEQUENTIAL_TEXT_REDACT_INPUT_TYPE_DRIFT = _d(
    "sequential_text_redact_input_type_drift", "Sequential text redact input type drift."
)
SPLIT_SINK_NEEDS_ALL_DISPATCHED = _d(
    "split_sink_needs_all_dispatched", "Split sink needs all dispatched."
)
TRANSFORM_SOURCE_MISSING = _d("transform_source_missing", "Transform source missing.")
UNDECLARED_OUTPUT_COLUMNS = _d("undeclared_output_columns", "Undeclared output columns.")
UNSUPPORTED_STRATEGY = _d("unsupported_strategy", "Unsupported strategy.")

_d = _catalog.runtime("FpeChecksumError")

FPE__CHECKSUM_INVALID_SOURCE = _d(
    "fpe.checksum_invalid_source",
    "FPE checksum invalid source.",
    symbol="FPE__CHECKSUM_INVALID_SOURCE",
)
FPE_CHECKSUM_UNSUPPORTED = _d("fpe.checksum_unsupported", "FPE checksum unsupported.")

_d = _catalog.runtime("FpeConfigError")

FPE_CONFIG_INVALID = _d("fpe.config_invalid", "FPE config invalid.")

_d = _catalog.runtime("FpeUnencryptableError")

FPE_UNENCRYPTABLE = _d("fpe.unencryptable", "FPE unencryptable.")
FPE__UNENCRYPTABLE_DOMAIN = _d(
    "fpe.unencryptable_domain", "FPE unencryptable domain.", symbol="FPE__UNENCRYPTABLE_DOMAIN"
)
FPE__UNENCRYPTABLE_LENGTH = _d(
    "fpe.unencryptable_length", "FPE unencryptable length.", symbol="FPE__UNENCRYPTABLE_LENGTH"
)

_d = _catalog.runtime("GenerationBudgetExceeded")

GENERATION_BUDGET_EXCEEDED = _d("generation_budget_exceeded", "Generation budget exceeded.")
VALUE_EXCEEDS_DECLARED_BOUND = _d("value_exceeds_declared_bound", "Value exceeds declared bound.")

_d = _catalog.runtime("GenerationError")

BUCKET_PERTURB_UNRECOGNIZED_BUCKET = _d(
    "bucket_perturb_unrecognized_bucket", "Bucket perturb unrecognized bucket."
)
BUNDLE_MISSING_OUTPUT_COLUMNS = _d(
    "bundle_missing_output_columns", "Bundle missing output columns."
)
CARDINALITY_TARGET_EXCEEDS_POOL = _d(
    "cardinality_target_exceeds_pool", "Cardinality target exceeds pool."
)
DETERMINISTIC_MODE_UNSUPPORTED_CARDINALITY = _d(
    "deterministic_mode_unsupported_cardinality", "Deterministic mode unsupported cardinality."
)
DETERMINISTIC_REQUIRES_SOURCE_AND_NAMESPACE = _d(
    "deterministic_requires_source_and_namespace", "Deterministic requires source and namespace."
)
EXACT_INT_NULL_MASK_MISMATCH = _d("exact_int_null_mask_mismatch", "Exact int null mask mismatch.")
FLOAT_CANONICALIZATION_UNSUPPORTED = _d(
    "float_canonicalization_unsupported", "Float canonicalization unsupported."
)
INDEX_BATCH_LENGTH_MISMATCH = _d("index_batch_length_mismatch", "Index batch length mismatch.")
INDEX_BATCH_NULL_MASK_MISMATCH = _d(
    "index_batch_null_mask_mismatch", "Index batch null mask mismatch."
)
INDEX_BATCH_OUT_OF_BOUNDS = _d("index_batch_out_of_bounds", "Index batch out of bounds.")
INDEX_BATCH_TYPE_MISMATCH = _d("index_batch_type_mismatch", "Index batch type mismatch.")
INVALID_SEED_LENGTH = _d("invalid_seed_length", "Invalid seed length.")
NATIVE_TYPE_NOT_ADMITTED = _d("native_type_not_admitted", "Native type not admitted.")
NULL_CANONICALIZATION_UNREACHABLE = _d(
    "null_canonicalization_unreachable", "Null canonicalization unreachable."
)
RAW_HEX_BATCH_LENGTH_MISMATCH = _d(
    "raw_hex_batch_length_mismatch", "Raw hex batch length mismatch."
)
RAW_HEX_BATCH_TYPE_MISMATCH = _d("raw_hex_batch_type_mismatch", "Raw hex batch type mismatch.")
RAW_HEX_BATCH_UNEXPECTED_NULL = _d(
    "raw_hex_batch_unexpected_null", "Raw hex batch unexpected null."
)
SOURCE_LENGTH_MISMATCH = _d("source_length_mismatch", "Source length mismatch.")
SOURCE_REQUIRED_FOR_CARDINALITY_MODE = _d(
    "source_required_for_cardinality_mode", "Source required for cardinality mode."
)
TIMEZONE_NAIVE_DATETIME = _d("timezone_naive_datetime", "Timezone naive datetime.")
UNIQUENESS_IMPOSSIBLE = _d("uniqueness_impossible", "Uniqueness impossible.")
UNKNOWN_CARDINALITY_MODE = _d("unknown_cardinality_mode", "Unknown cardinality mode.")

_d = _catalog.runtime("GenerationUnboundedError")

GENERATION_COLUMN_UNBOUNDED = _d("generation_column_unbounded", "Generation column unbounded.")

_d = _catalog.runtime("GenerationWindowError")

GENERATION_NOT_CLASS_A = _d("generation_not_class_a", "Generation not class a.")
GENERATION_TABLE_UNKNOWN = _d("generation_table_unknown", "Generation table unknown.")
GENERATION_WINDOW_INVALID = _d("generation_window_invalid", "Generation window invalid.")

_d = _catalog.runtime("GenerationWindowTimeoutError")

GENERATION_WINDOW_TIMEOUT = _d("generation_window_timeout", "Generation window timeout.")

_d = _catalog.runtime("KeyedStrategyRequiresSecret")

KEYED_STRATEGY_REQUIRES_SECRET = _d(
    "keyed_strategy_requires_secret",
    "Keyed strategy requires secret.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)

_d = _catalog.runtime("MaskKeyDerivationError")

MASK_KEY_DERIVATION_FAILED = _d(
    "mask.key_derivation_failed",
    "A per-column mask key could not be derived.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)

_d = _catalog.runtime("MaskKeyRequiredError")

MASK_KEY_REQUIRED = _d(
    "mask.key_required",
    "A keyed mask was run with no mask key.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)

_d = _catalog.runtime("MaskSecretError")

BAD_SECRET_REF = _d(
    "bad_secret_ref",
    "Bad secret ref.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)

_d = _catalog.runtime("MissingMaskSecret")

MISSING_MASK_SECRET = _d(
    "missing_mask_secret",
    "The mask secret could not be resolved.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)

_d = _catalog.runtime("MixedDeclineError")

GENERATION_SHAPE_UNSUPPORTED = _d("generation-shape-unsupported", "Generation shape unsupported.")
MIXED_DRIVER_UNSUPPORTED = _d("mixed-driver-unsupported", "Mixed driver unsupported.")
MIXED_FK_CROSS_GENERATE_UNSUPPORTED = _d(
    "mixed-fk-cross-generate-unsupported", "Mixed FK cross generate unsupported."
)
MIXED_FK_TOPOLOGY_UNSUPPORTED = _d(
    "mixed-fk-topology-unsupported", "Mixed FK topology unsupported."
)

_d = _catalog.runtime("NativeChunkSchemaDriftError")

NATIVE_CHUNK_SCHEMA_DRIFT = _d("native_chunk_schema_drift", "Native chunk schema drift.")

_d = _catalog.runtime("NativeCompanionCheckError")

NATIVE_COMPANION_CHECK_FAILED = _d(
    "native_companion.check_failed", "Native companion check failed."
)

_d = _catalog.runtime("PKDuplicatesError")

PK_DUPLICATES = _d(
    "pk.duplicates",
    "A primary-key column contains duplicate values.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)

_d = _catalog.runtime("PoolCapacityError")

POOL_CAPACITY_UNVERIFIABLE_NO_PROFILE = _d(
    "pool_capacity_unverifiable_no_profile", "Pool capacity unverifiable no profile."
)
POOL_EXCEEDS_CACHE_BUDGET = _d("pool_exceeds_cache_budget", "Pool exceeds cache budget.")
POOL_TOO_SMALL_FOR_SOURCE = _d("pool_too_small_for_source", "Pool too small for source.")
PROVIDER_NOT_POOLABLE = _d("provider_not_poolable", "Provider not poolable.")

_d = _catalog.runtime("PoolQualityError")

POOL_QUALITY_THRESHOLD_EXCEEDED = _d(
    "pool_quality.threshold_exceeded", "Pool quality threshold exceeded."
)

_d = _catalog.runtime("RowExceedsMaxRowBytes")

ROW_EXCEEDS_MAX_ROW_BYTES = _d("row_exceeds_max_row_bytes", "Row exceeds max row bytes.")

_d = _catalog.runtime("StrategyError")

BUCKET_PERTURB_INVALID_CONFIG = _d(
    "bucket_perturb_invalid_config", "Bucket perturb invalid config."
)
BUCKET_PERTURB_REQUIRES_NAMESPACE = _d(
    "bucket_perturb_requires_namespace", "Bucket perturb requires namespace."
)
BUCKETIZE_WIDTH_UNRESOLVABLE = _d("bucketize_width_unresolvable", "Bucketize width unresolvable.")
CATEGORICAL_CATEGORIES_NOT_LIST = _d(
    "categorical_categories_not_list", "Categorical categories not list."
)

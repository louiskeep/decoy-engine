"""Catalog of the ``runtime`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import RetryClass

__all__ = [
    "BOUNDED_READ_ERROR",
    "CAPACITY_SOURCE_UNPROFILABLE",
    "CHUNKED_FAKER_DETERMINISTIC_POOL_NOT_STRING",
    "CHUNKED_FAKER_NONDETERMINISTIC_POOL_NOT_STRING",
    "CHUNKED_FK_DECLARED_DTYPE_MISMATCH",
    "CHUNKED_FK_KEY_DTYPE_NOT_CROSS_ADAPTER_SAFE",
    "CHUNKED_LEADING_NULL_TYPE",
    "CHUNKED_PASSTHROUGH_VALUE_UNREPRESENTABLE",
    "CHUNKED_ROUTE_EVIDENCE_INCONSISTENT",
    "CHUNKED_ROUTE_EVIDENCE_MIXED_TABLES",
    "CHUNKED_ROW_OFFSET_OUT_OF_DOMAIN",
    "CHUNKED_SCHEMA_MISMATCH",
    "COMPOSITE_CUSTOM_BUNDLE_SHAPE",
    "COMPOSITE_FK_GROUP_NO_EDGE",
    "COMPOSITE_NAMESPACE_UNRESOLVED",
    "COMPOSITE_OUTPUT_COLUMN_MISSING",
    "CRYPTO_EXT_UNAVAILABLE",
    "CSV_MALFORMED",
    "CSV_RECORD_EXCEEDS_MAX_ROW_BYTES",
    "CYCLIC_WORK_ORDERING",
    "FK_FULL_FRAME_OOM_RISK_REJECTED",
    "FK_FULL_FRAME_OOM_RISK_REJECTED_ESTIMATED",
    "FPE_CHARSET_DEGENERATE",
    "FPE_CHARSET_DUPLICATE_SYMBOLS",
    "FPE_REQUIRES_NAMESPACE",
    "FULL_FRAME_VALUE_UNREPRESENTABLE",
    "GATED_HASH_ON_INTEGER_UNSUPPORTED",
    "GATED_STRING_OUTPUT_ON_NUMERIC_UNSUPPORTED",
    "HASH_REQUIRES_NAMESPACE",
    "HOLD_BACK_SPILL_UNAVAILABLE",
    "INTERNAL_PUBLISH_NOT_COMMITTED",
    "INVALID_EXECUTION_KNOB",
    "INVALID_NATIVE_THREADS",
    "LAZY_SOURCE_CHANGED",
    "LAZY_SOURCE_ROW_COUNT_MISMATCH",
    "MIXED_KEY_PAYLOAD_MISALIGNED",
    "MULTI_TABLE_RESULT_HAS_NO_SINGLE_OUTPUT",
    "NAMESPACE_EMPTY",
    "NULL_BEARING_INT_UNSUPPORTED",
    "ORPHAN_FK_VIOLATION",
    "ORPHAN_POLICY_CONFLICT",
    "ORPHAN_REMAP_PARENT_MISSING",
    "OUT_OF_CORE_BACKEND_UNAVAILABLE",
    "OUT_OF_CORE_BATCH_ROWS_INVALID",
    "OUT_OF_CORE_BUCKET_PERTURB_AUTODETECT_UNSUPPORTED",
    "OUT_OF_CORE_BUCKET_PERTURB_COLUMN_MISSING",
    "OUT_OF_CORE_BUCKET_PERTURB_DATE_OUT_OF_NATIVE_RANGE",
    "OUT_OF_CORE_BUCKET_PERTURB_SOURCE_TYPE_DRIFT",
    "OUT_OF_CORE_BUDGET_INVALID",
    "OUT_OF_CORE_CATEGORICAL_COLUMN_MISSING",
    "OUT_OF_CORE_CATEGORICAL_NONDETERMINISTIC_UNSUPPORTED",
    "OUT_OF_CORE_CATEGORICAL_SOURCE_TYPE_DRIFT",
    "OUT_OF_CORE_CATEGORICAL_UNPREPARED",
    "OUT_OF_CORE_CHILD_COLUMN_MISSING",
    "OUT_OF_CORE_CHILD_KEY_TYPES_MISMATCH",
    "OUT_OF_CORE_CODE_SET_ROW_ERROR_UNREACHABLE",
    "OUT_OF_CORE_CODE_SET_SHAPE_UNSUPPORTED",
    "OUT_OF_CORE_CONCURRENCY_INVALID",
    "OUT_OF_CORE_FAKER_COLUMN_MISSING",
    "OUT_OF_CORE_FAKER_INDEX_UNAVAILABLE",
    "OUT_OF_CORE_FAKER_POOL_BUDGET_EXCEEDED",
    "OUT_OF_CORE_FAKER_POOL_BUDGET_INVALID",
    "OUT_OF_CORE_FAKER_POOL_SIZE_INVALID",
    "OUT_OF_CORE_FAKER_POOL_UNSUPPORTED",
    "OUT_OF_CORE_FAKER_SOURCE_TYPE_DRIFT",
    "OUT_OF_CORE_FAKER_SOURCE_TYPE_UNSUPPORTED",
    "OUT_OF_CORE_FAKER_UNPREPARED",
    "OUT_OF_CORE_FANIN_EXCEEDS_BUDGET",
    "OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED",
    "OUT_OF_CORE_FK_KEY_DTYPE_UNSUPPORTED",
    "OUT_OF_CORE_FK_REORDER_CONTIGUITY",
    "OUT_OF_CORE_FK_ROW_ALIGNMENT",
    "OUT_OF_CORE_FPE_COLUMN_MISSING",
    "OUT_OF_CORE_FPE_COLUMN_REQUIRED",
    "OUT_OF_CORE_FPE_NATIVE_RESULT_INVALID",
    "OUT_OF_CORE_FPE_SOURCE_TYPE_DRIFT",
    "OUT_OF_CORE_FPE_UNPREPARED",
    "OUT_OF_CORE_HASH_COLUMN_MISSING",
    "OUT_OF_CORE_HASH_INPUT_TYPE_DRIFT",
    "OUT_OF_CORE_HASH_NATIVE_RESULT_INVALID",
    "OUT_OF_CORE_INSUFFICIENT_MEMORY",
    "OUT_OF_CORE_MEMORY_DETECTION_FAILED",
    "OUT_OF_CORE_PARENT_COLUMN_MISSING",
    "OUT_OF_CORE_PARENT_ROWS_UNRESOLVED",
    "OUT_OF_CORE_PARENT_SEED_MISSING",
    "OUT_OF_CORE_PARENT_STRATEGY_UNSUPPORTED",
    "OUT_OF_CORE_PREDICTED_SPILL_BYTES_INVALID",
    "OUT_OF_CORE_REJECTED",
    "OUT_OF_CORE_RELATIONSHIP_CYCLE",
    "OUT_OF_CORE_REMAP_SEEDS_MISSING",
    "OUT_OF_CORE_REMAP_VALUES_MISSING",
    "OUT_OF_CORE_REORDER_BUDGET_TOO_SMALL",
    "OUT_OF_CORE_REORDER_DISK_BUDGET_INVALID",
    "OUT_OF_CORE_REORDER_FAN_IN_INVALID",
    "OUT_OF_CORE_REORDER_HEAD_FIT_INVALID",
    "POOL_SIZE_INVALID",
    "POOL_SIZE_OVERFLOW",
    "SEED_WRONG_LENGTH",
    "SITE_NOT_PARTITIONABLE",
    "SITE_NOT_REPRODUCIBLE",
]

_d = _catalog.runtime("BoundedReadError")

BOUNDED_READ_ERROR = _d("bounded_read_error", "Bounded read error.")

_d = _catalog.runtime("CryptoExtensionUnavailableError")

CRYPTO_EXT_UNAVAILABLE = _d("crypto_ext.unavailable", "Crypto extension unavailable.")

_d = _catalog.runtime("CsvMalformed")

CSV_MALFORMED = _d("csv_malformed", "CSV malformed.")

_d = _catalog.runtime("CsvRecordExceedsMaxRowBytes")

CSV_RECORD_EXCEEDS_MAX_ROW_BYTES = _d(
    "csv_record_exceeds_max_row_bytes", "CSV record exceeds max row bytes."
)

_d = _catalog.runtime("DeterminismError")

NAMESPACE_EMPTY = _d("namespace_empty", "Namespace empty.")
POOL_SIZE_INVALID = _d("pool_size_invalid", "Pool size invalid.")
POOL_SIZE_OVERFLOW = _d("pool_size_overflow", "Pool size overflow.")
SEED_WRONG_LENGTH = _d("seed_wrong_length", "Seed wrong length.")

_d = _catalog.runtime("DrawSiteProtocolError")

SITE_NOT_PARTITIONABLE = _d("site_not_partitionable", "Site not partitionable.")
SITE_NOT_REPRODUCIBLE = _d("site_not_reproducible", "Site not reproducible.")

_d = _catalog.runtime("ExecutionError")

CAPACITY_SOURCE_UNPROFILABLE = _d("capacity_source_unprofilable", "Capacity source unprofilable.")
CHUNKED_FAKER_DETERMINISTIC_POOL_NOT_STRING = _d(
    "chunked_faker_deterministic_pool_not_string", "Chunked Faker deterministic pool not string."
)
CHUNKED_FAKER_NONDETERMINISTIC_POOL_NOT_STRING = _d(
    "chunked_faker_nondeterministic_pool_not_string",
    "Chunked Faker nondeterministic pool not string.",
)
CHUNKED_FK_DECLARED_DTYPE_MISMATCH = _d(
    "chunked_fk_declared_dtype_mismatch", "Chunked FK declared dtype mismatch."
)
CHUNKED_FK_KEY_DTYPE_NOT_CROSS_ADAPTER_SAFE = _d(
    "chunked_fk_key_dtype_not_cross_adapter_safe", "Chunked FK key dtype not cross adapter safe."
)
CHUNKED_LEADING_NULL_TYPE = _d("chunked_leading_null_type", "Chunked leading null type.")
CHUNKED_PASSTHROUGH_VALUE_UNREPRESENTABLE = _d(
    "chunked_passthrough_value_unrepresentable", "Chunked passthrough value unrepresentable."
)
CHUNKED_ROUTE_EVIDENCE_INCONSISTENT = _d(
    "chunked_route_evidence_inconsistent", "Chunked route evidence inconsistent."
)
CHUNKED_ROUTE_EVIDENCE_MIXED_TABLES = _d(
    "chunked_route_evidence_mixed_tables", "Chunked route evidence mixed tables."
)
CHUNKED_ROW_OFFSET_OUT_OF_DOMAIN = _d(
    "chunked_row_offset_out_of_domain", "Chunked row offset out of domain."
)
CHUNKED_SCHEMA_MISMATCH = _d("chunked_schema_mismatch", "Chunked schema mismatch.")
COMPOSITE_CUSTOM_BUNDLE_SHAPE = _d(
    "composite_custom_bundle_shape", "Composite custom bundle shape."
)
COMPOSITE_FK_GROUP_NO_EDGE = _d("composite_fk_group_no_edge", "Composite FK group no edge.")
COMPOSITE_NAMESPACE_UNRESOLVED = _d(
    "composite_namespace_unresolved", "Composite namespace unresolved."
)
COMPOSITE_OUTPUT_COLUMN_MISSING = _d(
    "composite_output_column_missing", "Composite output column missing."
)
CYCLIC_WORK_ORDERING = _d("cyclic_work_ordering", "Cyclic work ordering.")
FK_FULL_FRAME_OOM_RISK_REJECTED = _d(
    "fk_full_frame_oom_risk_rejected", "FK full frame OOM risk rejected."
)
FK_FULL_FRAME_OOM_RISK_REJECTED_ESTIMATED = _d(
    "fk_full_frame_oom_risk_rejected_estimated", "FK full frame OOM risk rejected estimated."
)
FPE_CHARSET_DEGENERATE = _d("fpe_charset_degenerate", "FPE charset degenerate.")
FPE_CHARSET_DUPLICATE_SYMBOLS = _d(
    "fpe_charset_duplicate_symbols", "FPE charset duplicate symbols."
)
FPE_REQUIRES_NAMESPACE = _d("fpe_requires_namespace", "FPE requires namespace.")
FULL_FRAME_VALUE_UNREPRESENTABLE = _d(
    "full_frame_value_unrepresentable", "Full frame value unrepresentable."
)
GATED_HASH_ON_INTEGER_UNSUPPORTED = _d(
    "gated_hash_on_integer_unsupported", "Gated hash on integer unsupported."
)
GATED_STRING_OUTPUT_ON_NUMERIC_UNSUPPORTED = _d(
    "gated_string_output_on_numeric_unsupported", "Gated string output on numeric unsupported."
)
HASH_REQUIRES_NAMESPACE = _d("hash_requires_namespace", "Hash requires namespace.")
HOLD_BACK_SPILL_UNAVAILABLE = _d("hold_back_spill_unavailable", "Hold back spill unavailable.")
INTERNAL_PUBLISH_NOT_COMMITTED = _d(
    "internal_publish_not_committed", "Internal publish not committed."
)
INVALID_EXECUTION_KNOB = _d("invalid_execution_knob", "Invalid execution knob.")
INVALID_NATIVE_THREADS = _d("invalid_native_threads", "Invalid native threads.")
LAZY_SOURCE_CHANGED = _d("lazy_source_changed", "Lazy source changed.")
LAZY_SOURCE_ROW_COUNT_MISMATCH = _d(
    "lazy_source_row_count_mismatch", "Lazy source row count mismatch."
)
MIXED_KEY_PAYLOAD_MISALIGNED = _d("mixed_key_payload_misaligned", "Mixed key payload misaligned.")
MULTI_TABLE_RESULT_HAS_NO_SINGLE_OUTPUT = _d(
    "multi_table_result_has_no_single_output", "Multi table result has no single output."
)
NULL_BEARING_INT_UNSUPPORTED = _d("null_bearing_int_unsupported", "Null bearing int unsupported.")
ORPHAN_FK_VIOLATION = _d("orphan_fk_violation", "Orphan FK violation.")
ORPHAN_POLICY_CONFLICT = _d("orphan_policy_conflict", "Orphan policy conflict.")
ORPHAN_REMAP_PARENT_MISSING = _d("orphan_remap_parent_missing", "Orphan remap parent missing.")
OUT_OF_CORE_BACKEND_UNAVAILABLE = _d(
    "out_of_core_backend_unavailable", "Out of core backend unavailable."
)
OUT_OF_CORE_BATCH_ROWS_INVALID = _d(
    "out_of_core_batch_rows_invalid", "Out of core batch rows invalid."
)
OUT_OF_CORE_BUCKET_PERTURB_AUTODETECT_UNSUPPORTED = _d(
    "out_of_core_bucket_perturb_autodetect_unsupported",
    "Out of core bucket perturb autodetect unsupported.",
)
OUT_OF_CORE_BUCKET_PERTURB_COLUMN_MISSING = _d(
    "out_of_core_bucket_perturb_column_missing", "Out of core bucket perturb column missing."
)
OUT_OF_CORE_BUCKET_PERTURB_DATE_OUT_OF_NATIVE_RANGE = _d(
    "out_of_core_bucket_perturb_date_out_of_native_range",
    "Out of core bucket perturb date out of native range.",
)
OUT_OF_CORE_BUCKET_PERTURB_SOURCE_TYPE_DRIFT = _d(
    "out_of_core_bucket_perturb_source_type_drift", "Out of core bucket perturb source type drift."
)
OUT_OF_CORE_BUDGET_INVALID = _d("out_of_core_budget_invalid", "Out of core budget invalid.")
OUT_OF_CORE_CATEGORICAL_COLUMN_MISSING = _d(
    "out_of_core_categorical_column_missing", "Out of core categorical column missing."
)
OUT_OF_CORE_CATEGORICAL_NONDETERMINISTIC_UNSUPPORTED = _d(
    "out_of_core_categorical_nondeterministic_unsupported",
    "Out of core categorical nondeterministic unsupported.",
)
OUT_OF_CORE_CATEGORICAL_SOURCE_TYPE_DRIFT = _d(
    "out_of_core_categorical_source_type_drift", "Out of core categorical source type drift."
)
OUT_OF_CORE_CATEGORICAL_UNPREPARED = _d(
    "out_of_core_categorical_unprepared", "Out of core categorical unprepared."
)
OUT_OF_CORE_CHILD_COLUMN_MISSING = _d(
    "out_of_core_child_column_missing", "Out of core child column missing."
)
OUT_OF_CORE_CHILD_KEY_TYPES_MISMATCH = _d(
    "out_of_core_child_key_types_mismatch", "Out of core child key types mismatch."
)
OUT_OF_CORE_CODE_SET_ROW_ERROR_UNREACHABLE = _d(
    "out_of_core_code_set_row_error_unreachable", "Out of core code set row error unreachable."
)
OUT_OF_CORE_CODE_SET_SHAPE_UNSUPPORTED = _d(
    "out_of_core_code_set_shape_unsupported", "Out of core code set shape unsupported."
)
OUT_OF_CORE_CONCURRENCY_INVALID = _d(
    "out_of_core_concurrency_invalid", "Out of core concurrency invalid."
)
OUT_OF_CORE_FAKER_COLUMN_MISSING = _d(
    "out_of_core_faker_column_missing", "Out of core Faker column missing."
)
OUT_OF_CORE_FAKER_INDEX_UNAVAILABLE = _d(
    "out_of_core_faker_index_unavailable", "Out of core Faker index unavailable."
)
OUT_OF_CORE_FAKER_POOL_BUDGET_EXCEEDED = _d(
    "out_of_core_faker_pool_budget_exceeded", "Out of core Faker pool budget exceeded."
)
OUT_OF_CORE_FAKER_POOL_BUDGET_INVALID = _d(
    "out_of_core_faker_pool_budget_invalid", "Out of core Faker pool budget invalid."
)
OUT_OF_CORE_FAKER_POOL_SIZE_INVALID = _d(
    "out_of_core_faker_pool_size_invalid", "Out of core Faker pool size invalid."
)
OUT_OF_CORE_FAKER_POOL_UNSUPPORTED = _d(
    "out_of_core_faker_pool_unsupported", "Out of core Faker pool unsupported."
)
OUT_OF_CORE_FAKER_SOURCE_TYPE_DRIFT = _d(
    "out_of_core_faker_source_type_drift", "Out of core Faker source type drift."
)
OUT_OF_CORE_FAKER_SOURCE_TYPE_UNSUPPORTED = _d(
    "out_of_core_faker_source_type_unsupported", "Out of core Faker source type unsupported."
)
OUT_OF_CORE_FAKER_UNPREPARED = _d("out_of_core_faker_unprepared", "Out of core Faker unprepared.")
OUT_OF_CORE_FANIN_EXCEEDS_BUDGET = _d(
    "out_of_core_fanin_exceeds_budget", "Out of core fanin exceeds budget."
)
OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED = _d(
    "out_of_core_fk_join_plan_unverified", "Out of core FK join plan unverified."
)
OUT_OF_CORE_FK_KEY_DTYPE_UNSUPPORTED = _d(
    "out_of_core_fk_key_dtype_unsupported", "Out of core FK key dtype unsupported."
)
OUT_OF_CORE_FK_REORDER_CONTIGUITY = _d(
    "out_of_core_fk_reorder_contiguity", "Out of core FK reorder contiguity."
)
OUT_OF_CORE_FK_ROW_ALIGNMENT = _d("out_of_core_fk_row_alignment", "Out of core FK row alignment.")
OUT_OF_CORE_FPE_COLUMN_MISSING = _d(
    "out_of_core_fpe_column_missing", "Out of core FPE column missing."
)
OUT_OF_CORE_FPE_COLUMN_REQUIRED = _d(
    "out_of_core_fpe_column_required", "Out of core FPE column required."
)
OUT_OF_CORE_FPE_NATIVE_RESULT_INVALID = _d(
    "out_of_core_fpe_native_result_invalid", "Out of core FPE native result invalid."
)
OUT_OF_CORE_FPE_SOURCE_TYPE_DRIFT = _d(
    "out_of_core_fpe_source_type_drift", "Out of core FPE source type drift."
)
OUT_OF_CORE_FPE_UNPREPARED = _d("out_of_core_fpe_unprepared", "Out of core FPE unprepared.")
OUT_OF_CORE_HASH_COLUMN_MISSING = _d(
    "out_of_core_hash_column_missing", "Out of core hash column missing."
)
OUT_OF_CORE_HASH_INPUT_TYPE_DRIFT = _d(
    "out_of_core_hash_input_type_drift", "Out of core hash input type drift."
)
OUT_OF_CORE_HASH_NATIVE_RESULT_INVALID = _d(
    "out_of_core_hash_native_result_invalid", "Out of core hash native result invalid."
)
OUT_OF_CORE_INSUFFICIENT_MEMORY = _d(
    "out_of_core_insufficient_memory",
    "Out of core insufficient memory.",
    retry=RetryClass.ENVIRONMENT_REPAIR,
)
OUT_OF_CORE_MEMORY_DETECTION_FAILED = _d(
    "out_of_core_memory_detection_failed",
    "Out of core memory detection failed.",
    retry=RetryClass.ENVIRONMENT_REPAIR,
)
OUT_OF_CORE_PARENT_COLUMN_MISSING = _d(
    "out_of_core_parent_column_missing", "Out of core parent column missing."
)
OUT_OF_CORE_PARENT_ROWS_UNRESOLVED = _d(
    "out_of_core_parent_rows_unresolved", "Out of core parent rows unresolved."
)
OUT_OF_CORE_PARENT_SEED_MISSING = _d(
    "out_of_core_parent_seed_missing", "Out of core parent seed missing."
)
OUT_OF_CORE_PARENT_STRATEGY_UNSUPPORTED = _d(
    "out_of_core_parent_strategy_unsupported", "Out of core parent strategy unsupported."
)
OUT_OF_CORE_PREDICTED_SPILL_BYTES_INVALID = _d(
    "out_of_core_predicted_spill_bytes_invalid", "Out of core predicted spill bytes invalid."
)
OUT_OF_CORE_REJECTED = _d("out_of_core_rejected", "Out of core rejected.")
OUT_OF_CORE_RELATIONSHIP_CYCLE = _d(
    "out_of_core_relationship_cycle", "Out of core relationship cycle."
)
OUT_OF_CORE_REMAP_SEEDS_MISSING = _d(
    "out_of_core_remap_seeds_missing", "Out of core remap seeds missing."
)
OUT_OF_CORE_REMAP_VALUES_MISSING = _d(
    "out_of_core_remap_values_missing", "Out of core remap values missing."
)
OUT_OF_CORE_REORDER_BUDGET_TOO_SMALL = _d(
    "out_of_core_reorder_budget_too_small", "Out of core reorder budget too small."
)
OUT_OF_CORE_REORDER_DISK_BUDGET_INVALID = _d(
    "out_of_core_reorder_disk_budget_invalid", "Out of core reorder disk budget invalid."
)
OUT_OF_CORE_REORDER_FAN_IN_INVALID = _d(
    "out_of_core_reorder_fan_in_invalid", "Out of core reorder fan in invalid."
)
OUT_OF_CORE_REORDER_HEAD_FIT_INVALID = _d(
    "out_of_core_reorder_head_fit_invalid", "Out of core reorder head fit invalid."
)

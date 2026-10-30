"""Catalog of the ``quality`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "DP_ADAPTER_AMBIGUOUS_COLUMN",
    "DP_ADAPTER_DUPLICATE_COLUMN",
    "DP_ADAPTER_MISSING_COLUMN",
    "DP_ADAPTER_SOURCE_TYPE",
    "DP_BUDGET_CATEGORICAL_PAIR_UNSCHEDULED",
    "DP_BUDGET_INFEASIBLE",
    "DP_CARRIER_BACKEND_UNSUPPORTED",
    "DP_CARRIER_BOUNDS_MISSING",
    "DP_CARRIER_BOUNDS_NONFINITE",
    "DP_CARRIER_BOUNDS_ORDER",
    "DP_CARRIER_BOUNDS_TYPE",
    "DP_CARRIER_FLAG_DTYPE",
    "DP_CARRIER_FLAG_LENGTH",
    "DP_CARRIER_INVALID",
    "DP_CARRIER_NUMBER_DTYPE",
    "DP_CARRIER_NUMBER_LENGTH",
    "DP_CARRIER_ROW_COUNT_NEGATIVE",
    "DP_CARRIER_ROW_COUNT_TYPE",
    "DP_CARRIER_SCHEMA_MISMATCH",
    "DP_CARRIER_TEXT_SHAPE",
    "DP_CARRIER_TYPE_MISMATCH",
    "DP_CARRIER_UNKNOWN",
    "DP_CARRIER_VALIDITY_LENGTH",
    "DP_CARRIER_VALIDITY_SHAPE",
    "DP_DELTA_INVALID",
    "DP_DUPLICATE_RELEASE",
    "DP_EPSILON_INVALID",
    "DP_EPSILON_UNSUPPORTED",
    "DP_KIND_CARRIER_MISMATCH",
    "DP_KIND_UNKNOWN",
    "DP_LOCK_INSTALLED_MISMATCH",
    "DP_LOCK_PARSE_ERROR",
    "DP_NUMERIC_BINS_INVALID",
    "DP_NUMERIC_DOMAIN_INVALID",
    "DP_PLATFORM_UNCERTIFIED",
    "DP_PROVENANCE_DISTRIBUTION_UNNAMED",
    "DP_PROVENANCE_DUPLICATE_DISTRIBUTION",
    "DP_PROVENANCE_RECORD_MALFORMED",
    "DP_SCHEDULE_INCOMPLETE",
    "DP_SCHEDULE_MISMATCH",
    "DP_SCHEMA_COLUMN_TYPE",
    "DP_SCHEMA_TYPE",
    "DP_STACK_UNCERTIFIED",
    "DP_UNSCHEDULED_RELEASE",
    "HIGH_CARDINALITY_AMBIGUOUS_STRING_COERCION",
    "HIGH_CARDINALITY_COLUMNS_NOT_COLLECTION",
    "HIGH_CARDINALITY_DISTINCT_LIMIT_EXCEEDED",
    "HIGH_CARDINALITY_INVALID_LABEL_ENCODING",
    "HIGH_CARDINALITY_LABEL_BYTES_LIMIT_EXCEEDED",
    "HIGH_CARDINALITY_NON_STRING_DTYPE",
]

_d = _catalog.quality("CarrierError")

DP_ADAPTER_AMBIGUOUS_COLUMN = _d("dp_adapter_ambiguous_column", "DP adapter ambiguous column.")
DP_ADAPTER_DUPLICATE_COLUMN = _d("dp_adapter_duplicate_column", "DP adapter duplicate column.")
DP_ADAPTER_MISSING_COLUMN = _d("dp_adapter_missing_column", "DP adapter missing column.")
DP_ADAPTER_SOURCE_TYPE = _d("dp_adapter_source_type", "DP adapter source type.")
DP_CARRIER_BOUNDS_MISSING = _d("dp_carrier_bounds_missing", "DP carrier bounds missing.")
DP_CARRIER_BOUNDS_NONFINITE = _d("dp_carrier_bounds_nonfinite", "DP carrier bounds nonfinite.")
DP_CARRIER_BOUNDS_ORDER = _d("dp_carrier_bounds_order", "DP carrier bounds order.")
DP_CARRIER_BOUNDS_TYPE = _d("dp_carrier_bounds_type", "DP carrier bounds type.")
DP_CARRIER_FLAG_DTYPE = _d("dp_carrier_flag_dtype", "DP carrier flag dtype.")
DP_CARRIER_FLAG_LENGTH = _d("dp_carrier_flag_length", "DP carrier flag length.")
DP_CARRIER_NUMBER_DTYPE = _d("dp_carrier_number_dtype", "DP carrier number dtype.")
DP_CARRIER_NUMBER_LENGTH = _d("dp_carrier_number_length", "DP carrier number length.")
DP_CARRIER_ROW_COUNT_NEGATIVE = _d(
    "dp_carrier_row_count_negative", "DP carrier row count negative."
)
DP_CARRIER_ROW_COUNT_TYPE = _d("dp_carrier_row_count_type", "DP carrier row count type.")
DP_CARRIER_SCHEMA_MISMATCH = _d("dp_carrier_schema_mismatch", "DP carrier schema mismatch.")
DP_CARRIER_TEXT_SHAPE = _d("dp_carrier_text_shape", "DP carrier text shape.")
DP_CARRIER_TYPE_MISMATCH = _d("dp_carrier_type_mismatch", "DP carrier type mismatch.")
DP_CARRIER_UNKNOWN = _d("dp_carrier_unknown", "DP carrier unknown.")
DP_CARRIER_VALIDITY_LENGTH = _d("dp_carrier_validity_length", "DP carrier validity length.")
DP_CARRIER_VALIDITY_SHAPE = _d("dp_carrier_validity_shape", "DP carrier validity shape.")
DP_KIND_CARRIER_MISMATCH = _d("dp_kind_carrier_mismatch", "DP kind carrier mismatch.")
DP_KIND_UNKNOWN = _d("dp_kind_unknown", "DP kind unknown.")
DP_SCHEMA_COLUMN_TYPE = _d("dp_schema_column_type", "DP schema column type.")
DP_SCHEMA_TYPE = _d("dp_schema_type", "DP schema type.")

_d = _catalog.quality("DistributionSnapshotError")

HIGH_CARDINALITY_AMBIGUOUS_STRING_COERCION = _d(
    "high_cardinality_ambiguous_string_coercion", "High cardinality ambiguous string coercion."
)
HIGH_CARDINALITY_COLUMNS_NOT_COLLECTION = _d(
    "high_cardinality_columns_not_collection", "High cardinality columns not collection."
)
HIGH_CARDINALITY_DISTINCT_LIMIT_EXCEEDED = _d(
    "high_cardinality_distinct_limit_exceeded", "High cardinality distinct limit exceeded."
)
HIGH_CARDINALITY_INVALID_LABEL_ENCODING = _d(
    "high_cardinality_invalid_label_encoding", "High cardinality invalid label encoding."
)
HIGH_CARDINALITY_LABEL_BYTES_LIMIT_EXCEEDED = _d(
    "high_cardinality_label_bytes_limit_exceeded", "High cardinality label bytes limit exceeded."
)
HIGH_CARDINALITY_NON_STRING_DTYPE = _d(
    "high_cardinality_non_string_dtype", "High cardinality non string dtype."
)

_d = _catalog.quality("DpBudgetError")

DP_BUDGET_CATEGORICAL_PAIR_UNSCHEDULED = _d(
    "dp_budget_categorical_pair_unscheduled", "DP budget categorical pair unscheduled."
)
DP_BUDGET_INFEASIBLE = _d("dp_budget_infeasible", "DP budget infeasible.")
DP_CARRIER_BACKEND_UNSUPPORTED = _d(
    "dp_carrier_backend_unsupported", "DP carrier backend unsupported."
)
DP_CARRIER_INVALID = _d("dp_carrier_invalid", "DP carrier invalid.")
DP_DUPLICATE_RELEASE = _d("dp_duplicate_release", "DP duplicate release.")
DP_EPSILON_UNSUPPORTED = _d("dp_epsilon_unsupported", "DP epsilon unsupported.")
DP_SCHEDULE_INCOMPLETE = _d("dp_schedule_incomplete", "DP schedule incomplete.")
DP_SCHEDULE_MISMATCH = _d("dp_schedule_mismatch", "DP schedule mismatch.")
DP_UNSCHEDULED_RELEASE = _d("dp_unscheduled_release", "DP unscheduled release.")

_d = _catalog.quality("DpError")

DP_DELTA_INVALID = _d("dp_delta_invalid", "DP delta invalid.")
DP_EPSILON_INVALID = _d("dp_epsilon_invalid", "DP epsilon invalid.")
DP_NUMERIC_BINS_INVALID = _d("dp_numeric_bins_invalid", "DP numeric bins invalid.")
DP_NUMERIC_DOMAIN_INVALID = _d("dp_numeric_domain_invalid", "DP numeric domain invalid.")

_d = _catalog.quality("ProvenanceError")

DP_LOCK_INSTALLED_MISMATCH = _d("dp_lock_installed_mismatch", "DP lock installed mismatch.")
DP_LOCK_PARSE_ERROR = _d("dp_lock_parse_error", "DP lock parse error.")
DP_PLATFORM_UNCERTIFIED = _d("dp_platform_uncertified", "DP platform uncertified.")
DP_PROVENANCE_DISTRIBUTION_UNNAMED = _d(
    "dp_provenance_distribution_unnamed", "DP provenance distribution unnamed."
)
DP_PROVENANCE_DUPLICATE_DISTRIBUTION = _d(
    "dp_provenance_duplicate_distribution", "DP provenance duplicate distribution."
)
DP_PROVENANCE_RECORD_MALFORMED = _d(
    "dp_provenance_record_malformed", "DP provenance record malformed."
)
DP_STACK_UNCERTIFIED = _d("dp_stack_uncertified", "DP stack uncertified.")

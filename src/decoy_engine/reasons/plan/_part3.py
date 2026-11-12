"""Catalog of the ``plan`` namespace, part 3."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import UNRELEASED

__all__ = [
    "DEDUPE_UNSUPPORTED_TYPE",
    "GENERATE_CATEGORIES_MIXED",
    "GENERATE_COLUMN_FORWARD_REFERENCE",
    "GENERATE_FAKER_KWARGS_UNKNOWN",
    "GENERATE_FAKER_TYPE_UNKNOWN",
    "GENERATE_FORMULA_NAME_UNKNOWN",
    "GENERATE_FORMULA_REFERENCE_MISSING",
    "GENERATE_FORMULA_REFERENCE_UNKNOWN",
    "GENERATE_FORMULA_TEMPLATE_SYNTAX",
    "GENERATE_LOCALE_UNKNOWN",
    "GENERATE_OPTION_INVALID",
    "GROUPED_SERIES_GROUP_BY_EQUALS_ORDER_BY",
    "LOOKUP_COLUMN_MISSING",
    "LOOKUP_FILE_EMPTY",
    "LOOKUP_FILE_FORMAT_UNSUPPORTED",
    "LOOKUP_FILE_TOO_LARGE",
    "LOOKUP_FILE_UNREADABLE",
    "LOOKUP_NOT_PINNED",
    "LOOKUP_PINNED_DIGEST_MISMATCH",
    "LOOKUP_TOO_MANY_ENTRIES",
    "LOOKUP_WEIGHT_INVALID",
    "PINNED_LOOKUP_UNSUPPORTED",
    "REFERENCE_WEIGHTS_LENGTH_MISMATCH",
    "RELATIONSHIP_PARENT_NOT_UNIQUE",
    "SORT_UNSUPPORTED_TYPE",
    "STATISTICAL_HIGH_CARDINALITY_REQUIRES_REAL_CATEGORIES",
    "STATISTICAL_HIGH_CARDINALITY_SNAPSHOT_MISMATCH",
    "STATISTICAL_JOINT_MISSING",
    "STATISTICAL_KIND_UNSUPPORTED",
    "STATISTICAL_OTHER_MODE_INVALID",
    "STATISTICAL_REAL_CATEGORIES_NOT_ALLOWED",
    "STATISTICAL_SNAPSHOT_INDEX_OUT_OF_RANGE",
    "STATISTICAL_SNAPSHOT_SCHEMA_MISMATCH",
    "STATISTICAL_SNAPSHOT_UNREADABLE",
    "STATISTICAL_SNAPSHOT_VALUES_REDACTED",
    "STATISTICAL_SPEC_NOT_PINNED",
    "STATISTICAL_STATS_DEGENERATE",
    "TOP_CODE_INVALID_FLOOR",
]

_d = _catalog.plan("StatisticalSpecError")

STATISTICAL_HIGH_CARDINALITY_REQUIRES_REAL_CATEGORIES = _d(
    "statistical_high_cardinality_requires_real_categories",
    "Statistical high cardinality requires real categories.",
)
STATISTICAL_HIGH_CARDINALITY_SNAPSHOT_MISMATCH = _d(
    "statistical_high_cardinality_snapshot_mismatch",
    "Statistical high cardinality snapshot mismatch.",
)
STATISTICAL_JOINT_MISSING = _d("statistical_joint_missing", "Statistical joint missing.")
STATISTICAL_KIND_UNSUPPORTED = _d("statistical_kind_unsupported", "Statistical kind unsupported.")
STATISTICAL_OTHER_MODE_INVALID = _d(
    "statistical_other_mode_invalid", "Statistical other mode invalid."
)
STATISTICAL_REAL_CATEGORIES_NOT_ALLOWED = _d(
    "statistical_real_categories_not_allowed", "Statistical real categories not allowed."
)
STATISTICAL_SNAPSHOT_INDEX_OUT_OF_RANGE = _d(
    "statistical_snapshot_index_out_of_range", "Statistical snapshot index out of range."
)
STATISTICAL_SNAPSHOT_SCHEMA_MISMATCH = _d(
    "statistical_snapshot_schema_mismatch", "Statistical snapshot schema mismatch."
)
STATISTICAL_SNAPSHOT_UNREADABLE = _d(
    "statistical_snapshot_unreadable", "Statistical snapshot unreadable."
)
STATISTICAL_SNAPSHOT_VALUES_REDACTED = _d(
    "statistical_snapshot_values_redacted",
    "Statistical snapshot values redacted.",
    since="1.11.0",
)
STATISTICAL_SPEC_NOT_PINNED = _d("statistical_spec_not_pinned", "Statistical spec not pinned.")
STATISTICAL_STATS_DEGENERATE = _d("statistical_stats_degenerate", "Statistical stats degenerate.")

_d = _catalog.plan("decoy_engine.execution._transforms")

DEDUPE_UNSUPPORTED_TYPE = _d("dedupe_unsupported_type", "Dedupe unsupported type.")
SORT_UNSUPPORTED_TYPE = _d("sort_unsupported_type", "Sort unsupported type.")

_d = _catalog.plan("decoy_engine.plan._checks_top_code")

TOP_CODE_INVALID_FLOOR = _d("top_code_invalid_floor", "Top code invalid floor.")

# Summaries double as the refusal text plan checks show, so they never name a value.
_d = _catalog.plan("PlanCompileError")

GENERATE_CATEGORIES_MIXED = _d(
    "generate_categories_mixed",
    "Generate column categories mix value types.",
    since="1.13.0",
)
GENERATE_COLUMN_FORWARD_REFERENCE = _d(
    "generate_column_forward_reference",
    "Generate column reads a column that is not ready yet.",
    since="1.13.0",
)
GENERATE_FAKER_KWARGS_UNKNOWN = _d(
    "generate_faker_kwargs_unknown",
    "Generate column passes an argument the Faker provider does not take.",
    since="1.13.0",
)
GENERATE_FAKER_TYPE_UNKNOWN = _d(
    "generate_faker_type_unknown",
    "Generate column names a Faker provider that does not exist.",
    since="1.13.0",
)
GENERATE_FORMULA_NAME_UNKNOWN = _d(
    "generate_formula_name_unknown",
    "Generate column formula uses a name that is not defined.",
    since="1.13.0",
)
GENERATE_FORMULA_REFERENCE_MISSING = _d(
    "generate_formula_reference_missing",
    "Generate column formula reads a sibling column not listed in its references.",
    since="1.13.0",
)
GENERATE_FORMULA_REFERENCE_UNKNOWN = _d(
    "generate_formula_reference_unknown",
    "Generate column formula references a column that is not a sibling generate column.",
    since="1.13.0",
)
GENERATE_FORMULA_TEMPLATE_SYNTAX = _d(
    "generate_formula_template_syntax",
    "Generate column formula wraps a column name in braces; use the bare name.",
    since="1.13.0",
)
GENERATE_LOCALE_UNKNOWN = _d(
    "generate_locale_unknown",
    "Generate column locale is not a known Faker locale.",
    since="1.13.0",
)
GENERATE_OPTION_INVALID = _d(
    "generate_option_invalid",
    "Generate column option is not valid for its column type.",
    since="1.13.0",
)
GROUPED_SERIES_GROUP_BY_EQUALS_ORDER_BY = _d(
    "grouped_series_group_by_equals_order_by",
    "Grouped series group_by and order_by name the same column.",
    since="1.13.0",
)
REFERENCE_WEIGHTS_LENGTH_MISMATCH = _d(
    "reference_weights_length_mismatch",
    "Reference weights count does not match the parent sequence row count.",
    since="1.13.0",
)
RELATIONSHIP_PARENT_NOT_UNIQUE = _d(
    "relationship_parent_not_unique",
    "A generated relationship parent key is not provably unique.",
    since=UNRELEASED,
)

_d = _catalog.plan("PlanCompileError")

LOOKUP_COLUMN_MISSING = _d(
    "lookup_column_missing",
    "Lookup file does not have the value or weight column the generate column names.",
    since=UNRELEASED,
)
LOOKUP_FILE_EMPTY = _d(
    "lookup_file_empty",
    "Lookup file has no entries.",
    since=UNRELEASED,
)
LOOKUP_FILE_FORMAT_UNSUPPORTED = _d(
    "lookup_file_format_unsupported",
    "Lookup file is not a CSV or Parquet file with a value column of a supported type.",
    since=UNRELEASED,
)
LOOKUP_FILE_TOO_LARGE = _d(
    "lookup_file_too_large",
    "Lookup file is larger than the embedded-artifact size cap.",
    since=UNRELEASED,
)
LOOKUP_FILE_UNREADABLE = _d(
    "lookup_file_unreadable",
    "Lookup file could not be read or parsed.",
    since=UNRELEASED,
)
LOOKUP_NOT_PINNED = _d(
    "lookup_not_pinned",
    "A lookup file named by the plan's configuration is not among its pinned lookups.",
    since=UNRELEASED,
)
LOOKUP_PINNED_DIGEST_MISMATCH = _d(
    "lookup_pinned_digest_mismatch",
    "A pinned lookup file does not match its recorded sha256.",
    since=UNRELEASED,
)
LOOKUP_TOO_MANY_ENTRIES = _d(
    "lookup_too_many_entries",
    "Lookup file has more entries than the 1,000,000 entry cap.",
    since=UNRELEASED,
)
LOOKUP_WEIGHT_INVALID = _d(
    "lookup_weight_invalid",
    "Lookup file has a weight that is not a finite number of at least zero, or no positive total.",
    since=UNRELEASED,
)

_d = _catalog.plan("PinnedModeError")

PINNED_LOOKUP_UNSUPPORTED = _d(
    "pinned_lookup_unsupported",
    "Lookup columns are not available in pinned mode.",
    since=UNRELEASED,
)

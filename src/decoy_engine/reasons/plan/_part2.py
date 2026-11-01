"""Catalog of the ``plan`` namespace, part 2."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "DERIVED_BOUNDS_INVERTED",
    "DERIVED_CYCLIC_REFERENCE",
    "DERIVED_EXPRESSION_MISSING",
    "DERIVED_MISSING_COLUMN_REF",
    "DERIVED_NULL_PROPAGATION_INVALID",
    "DETERMINISTIC_NAMESPACE_MISSING",
    "DP_BUDGET_DECLARATION_MALFORMED",
    "DP_BUDGET_EXCEEDED",
    "DP_FLAG_TOKEN_INVALID",
    "DP_GENERATE_ALLOW_REAL_CATEGORIES_UNSUPPORTED",
    "DP_GENERATE_HIGH_CARDINALITY_UNSUPPORTED",
    "DP_JOINT_UNSUPPORTED",
    "DP_PINNED_SNAPSHOT_DIGEST_MISMATCH",
    "DP_PINNED_SNAPSHOT_PAYLOAD_NOT_JSON",
    "DP_SNAPSHOT_CODEC_UNSUPPORTED",
    "DP_SNAPSHOT_EMBEDDED_ARTIFACT_TOO_LARGE",
    "DP_SNAPSHOT_NUMERIC_EDGES_MISMATCH",
    "DP_SNAPSHOT_NUMERIC_SHAPE_MISMATCH",
    "DP_SNAPSHOT_RELEASE_SHAPE_UNSUPPORTED",
    "FAKER_REQUIRES_PROVIDER",
    "FK_CYCLE",
    "FPE_CHARSET_DEGENERATE",
    "FPE_CHARSET_DUPLICATE_SYMBOLS",
    "FPE_CHARSET_NON_ASCII",
    "FPE_CHECKSUM_CHARSET_INCOMPATIBLE",
    "FPE_CHECKSUM_IBAN_UNSUPPORTED",
    "FPE_CHECKSUM_UNKNOWN_SCHEME",
    "FPE_JOIN_GROUP_CONFIG_MISMATCH",
    "FPE_JOIN_GROUP_NAMESPACE_MISMATCH",
    "FPE_JOIN_GROUP_NON_FPE_MEMBER",
    "FPE_JOIN_GROUP_SINGLETON",
    "GEO_GENERALIZE_INVALID_CASCADE",
    "GEO_GENERALIZE_INVALID_CASCADE_LEVEL",
    "GEO_GENERALIZE_MISSING_SUPPRESS",
    "GEO_GENERALIZE_UNSUPPORTED_TYPE",
    "GROUPED_SERIES_GENERATOR_INVALID",
    "GROUPED_SERIES_GROUP_BY_MISSING",
    "GROUPED_SERIES_MISSING_GROUP_BY_REF",
    "GROUPED_SERIES_MISSING_ORDER_BY_REF",
    "GROUPED_SERIES_ORDER_BY_MISSING",
    "GROUPED_SERIES_STEP_OUT_OF_RANGE",
    "GROUP_KEY_GROUP_BY_MISSING",
    "GROUP_KEY_LENGTH_ODD",
    "GROUP_KEY_LENGTH_OUT_OF_RANGE",
    "GROUP_KEY_MISSING_GROUP_BY_REF",
    "JOINT_MASK_COLUMNS_MISSING",
    "JOINT_MASK_COLUMN_NOT_IN_REFERENCE",
    "JOINT_MASK_KEY_BY_MISSING",
    "JOINT_MASK_REFERENCE_INVALID",
    "JOINT_MASK_REFERENCE_MISSING",
    "JOINT_MASK_REFERENCE_NOT_FOUND",
    "NON_POOLABLE_PROVIDER_WITH_POOL_BACKEND",
    "NULL_BEARING_INT_UNSUPPORTED",
    "ORPHAN_FK_POLICY_DUPLICATE",
    "ORPHAN_FK_POLICY_INVALID",
    "ORPHAN_FK_POLICY_MISSING",
    "ORPHAN_POLICY_CONFLICT",
    "PER_TABLE_TRANSFORMS_PRESENT",
    "PLAN_SCHEMA_DETERMINISTIC_MAP_RENAMED",
    "POOL_CAPACITY_PRE_FLIGHT_UNIQUE",
    "POOL_SIZE_LOCATION_CONFLICT",
    "SEED_NOT_NUMERIC",
    "SEED_OVERFLOW",
    "STATISTICAL_ALLOW_REAL_CATEGORIES_INVALID_TYPE",
    "STATISTICAL_COLUMN_NOT_IN_SNAPSHOT",
    "STATISTICAL_CONDITION_COLUMN_UNAVAILABLE",
    "STATISTICAL_CONDITION_KIND_INVALID",
    "STATISTICAL_DP_CARRIER_INVALID",
    "STATISTICAL_FLAG_OTHER_MODE_EMIT_INVALID",
    "STATISTICAL_FLAG_REQUIRES_DP_DECLARATION",
    "STATISTICAL_FLAG_TOKEN_INVALID",
    "STATISTICAL_HIGH_CARDINALITY_INVALID_TYPE",
    "STATISTICAL_HIGH_CARDINALITY_KIND_INVALID",
    "STATISTICAL_HIGH_CARDINALITY_WRONG_TYPE",
    "STATISTICAL_SNAPSHOT_FILE_REQUIRED",
    "STATISTICAL_SNAPSHOT_NOT_PINNED",
    "STRATEGY_NOT_CHUNK_SAFE",
    "TOP_CODE_BOUNDS_UNRESOLVABLE",
    "TOP_CODE_FLOOR_GE_CAP",
    "TOP_CODE_MISSING_OVER_LABEL",
    "TOP_CODE_MISSING_UNDER_LABEL",
    "TOP_CODE_UNDER_LABEL_WITHOUT_FLOOR",
    "TOP_CODE_UNSUPPORTED_IN_NESTED",
    "TOP_CODE_WITH_WHEN_UNSUPPORTED",
    "TRUNCATE_KEEP_INVALID",
    "TRUNCATE_LENGTH_INVALID",
    "TRUNCATE_MASK_CHAR_INVALID",
    "UNKNOWN_PROVIDER",
    "VAULT_REQUIRES_CRYPTOGRAPHY",
    "VAULT_REQUIRES_NAMESPACE",
    "VAULT_STRATEGY_REVERSIBLE",
    "VAULT_STRATEGY_UNMASKED",
    "WHEN_OUTSIDE_CLOSED_GRAMMAR",
    "WHEN_STRING_OUTPUT_ON_NUMERIC_UNSUPPORTED",
    "WHEN_WITH_COHERENT_WITH_UNSUPPORTED",
    "WINDOWED_DATE_ANCHOR_MISSING",
    "WINDOWED_DATE_DISTRIBUTION_INVALID",
    "WINDOWED_DATE_MAX_DAYS_MISSING",
    "WINDOWED_DATE_MIN_EXCEEDS_MAX",
    "WINDOWED_DATE_MISSING_ANCHOR_REF",
]

_d = _catalog.plan("PlanCompileError")

DERIVED_BOUNDS_INVERTED = _d("derived_bounds_inverted", "Derived bounds inverted.")
DERIVED_CYCLIC_REFERENCE = _d("derived_cyclic_reference", "Derived cyclic reference.")
DERIVED_EXPRESSION_MISSING = _d("derived_expression_missing", "Derived expression missing.")
DERIVED_MISSING_COLUMN_REF = _d("derived_missing_column_ref", "Derived missing column ref.")
DERIVED_NULL_PROPAGATION_INVALID = _d(
    "derived_null_propagation_invalid", "Derived null propagation invalid."
)
DETERMINISTIC_NAMESPACE_MISSING = _d(
    "deterministic_namespace_missing", "Deterministic namespace missing."
)
DP_BUDGET_DECLARATION_MALFORMED = _d(
    "dp_budget_declaration_malformed", "DP budget declaration malformed."
)
DP_BUDGET_EXCEEDED = _d("dp_budget_exceeded", "DP budget exceeded.")
DP_FLAG_TOKEN_INVALID = _d("dp_flag_token_invalid", "DP flag token invalid.")
DP_GENERATE_ALLOW_REAL_CATEGORIES_UNSUPPORTED = _d(
    "dp_generate_allow_real_categories_unsupported",
    "DP generate allow real categories unsupported.",
)
DP_GENERATE_HIGH_CARDINALITY_UNSUPPORTED = _d(
    "dp_generate_high_cardinality_unsupported", "DP generate high cardinality unsupported."
)
DP_JOINT_UNSUPPORTED = _d("dp_joint_unsupported", "DP joint unsupported.")
DP_PINNED_SNAPSHOT_DIGEST_MISMATCH = _d(
    "dp_pinned_snapshot_digest_mismatch", "DP pinned snapshot digest mismatch."
)
DP_PINNED_SNAPSHOT_PAYLOAD_NOT_JSON = _d(
    "dp_pinned_snapshot_payload_not_json", "DP pinned snapshot payload not JSON."
)
DP_SNAPSHOT_CODEC_UNSUPPORTED = _d(
    "dp_snapshot_codec_unsupported", "DP snapshot codec unsupported."
)
DP_SNAPSHOT_EMBEDDED_ARTIFACT_TOO_LARGE = _d(
    "dp_snapshot_embedded_artifact_too_large", "DP snapshot embedded artifact too large."
)
DP_SNAPSHOT_NUMERIC_EDGES_MISMATCH = _d(
    "dp_snapshot_numeric_edges_mismatch", "DP snapshot numeric edges mismatch."
)
DP_SNAPSHOT_NUMERIC_SHAPE_MISMATCH = _d(
    "dp_snapshot_numeric_shape_mismatch", "DP snapshot numeric shape mismatch."
)
DP_SNAPSHOT_RELEASE_SHAPE_UNSUPPORTED = _d(
    "dp_snapshot_release_shape_unsupported", "DP snapshot release shape unsupported."
)
FAKER_REQUIRES_PROVIDER = _d("faker_requires_provider", "Faker requires provider.")
FK_CYCLE = _d("fk_cycle", "FK cycle.")
FPE_CHARSET_DEGENERATE = _d("fpe_charset_degenerate", "FPE charset degenerate.")
FPE_CHARSET_DUPLICATE_SYMBOLS = _d(
    "fpe_charset_duplicate_symbols", "FPE charset duplicate symbols."
)
FPE_CHARSET_NON_ASCII = _d("fpe_charset_non_ascii", "FPE charset non ascii.")
FPE_CHECKSUM_CHARSET_INCOMPATIBLE = _d(
    "fpe_checksum_charset_incompatible", "FPE checksum charset incompatible."
)
FPE_CHECKSUM_IBAN_UNSUPPORTED = _d(
    "fpe_checksum_iban_unsupported", "FPE checksum IBAN unsupported."
)
FPE_CHECKSUM_UNKNOWN_SCHEME = _d("fpe_checksum_unknown_scheme", "FPE checksum unknown scheme.")
FPE_JOIN_GROUP_CONFIG_MISMATCH = _d(
    "fpe_join_group_config_mismatch", "FPE join group config mismatch."
)
FPE_JOIN_GROUP_NAMESPACE_MISMATCH = _d(
    "fpe_join_group_namespace_mismatch", "FPE join group namespace mismatch."
)
FPE_JOIN_GROUP_NON_FPE_MEMBER = _d(
    "fpe_join_group_non_fpe_member", "FPE join group non FPE member."
)
FPE_JOIN_GROUP_SINGLETON = _d("fpe_join_group_singleton", "FPE join group singleton.")
GEO_GENERALIZE_INVALID_CASCADE = _d(
    "geo_generalize_invalid_cascade", "Geo generalize invalid cascade."
)
GEO_GENERALIZE_INVALID_CASCADE_LEVEL = _d(
    "geo_generalize_invalid_cascade_level", "Geo generalize invalid cascade level."
)
GEO_GENERALIZE_MISSING_SUPPRESS = _d(
    "geo_generalize_missing_suppress", "Geo generalize missing suppress."
)
GEO_GENERALIZE_UNSUPPORTED_TYPE = _d(
    "geo_generalize_unsupported_type", "Geo generalize unsupported type."
)
GROUP_KEY_GROUP_BY_MISSING = _d("group_key_group_by_missing", "Group key group by missing.")
GROUP_KEY_LENGTH_ODD = _d("group_key_length_odd", "Group key length odd.")
GROUP_KEY_LENGTH_OUT_OF_RANGE = _d(
    "group_key_length_out_of_range", "Group key length out of range."
)
GROUP_KEY_MISSING_GROUP_BY_REF = _d(
    "group_key_missing_group_by_ref", "Group key missing group by ref."
)
GROUPED_SERIES_GENERATOR_INVALID = _d(
    "grouped_series_generator_invalid", "Grouped series generator invalid."
)
GROUPED_SERIES_GROUP_BY_MISSING = _d(
    "grouped_series_group_by_missing", "Grouped series group by missing."
)
GROUPED_SERIES_MISSING_GROUP_BY_REF = _d(
    "grouped_series_missing_group_by_ref", "Grouped series missing group by ref."
)
GROUPED_SERIES_MISSING_ORDER_BY_REF = _d(
    "grouped_series_missing_order_by_ref", "Grouped series missing order by ref."
)
GROUPED_SERIES_ORDER_BY_MISSING = _d(
    "grouped_series_order_by_missing", "Grouped series order by missing."
)
GROUPED_SERIES_STEP_OUT_OF_RANGE = _d(
    "grouped_series_step_out_of_range", "Grouped series step out of range."
)
JOINT_MASK_COLUMN_NOT_IN_REFERENCE = _d(
    "joint_mask_column_not_in_reference", "Joint mask column not in reference."
)
JOINT_MASK_COLUMNS_MISSING = _d("joint_mask_columns_missing", "Joint mask columns missing.")
JOINT_MASK_KEY_BY_MISSING = _d("joint_mask_key_by_missing", "Joint mask key by missing.")
JOINT_MASK_REFERENCE_INVALID = _d("joint_mask_reference_invalid", "Joint mask reference invalid.")
JOINT_MASK_REFERENCE_MISSING = _d("joint_mask_reference_missing", "Joint mask reference missing.")
JOINT_MASK_REFERENCE_NOT_FOUND = _d(
    "joint_mask_reference_not_found", "Joint mask reference not found."
)
NON_POOLABLE_PROVIDER_WITH_POOL_BACKEND = _d(
    "non_poolable_provider_with_pool_backend", "Non poolable provider with pool backend."
)
NULL_BEARING_INT_UNSUPPORTED = _d("null_bearing_int_unsupported", "Null bearing int unsupported.")
ORPHAN_FK_POLICY_DUPLICATE = _d("orphan_fk_policy_duplicate", "Orphan FK policy duplicate.")
ORPHAN_FK_POLICY_INVALID = _d("orphan_fk_policy_invalid", "Orphan FK policy invalid.")
ORPHAN_FK_POLICY_MISSING = _d("orphan_fk_policy_missing", "Orphan FK policy missing.")
ORPHAN_POLICY_CONFLICT = _d("orphan_policy_conflict", "Orphan policy conflict.")
PER_TABLE_TRANSFORMS_PRESENT = _d("per_table_transforms_present", "Per table transforms present.")
PLAN_SCHEMA_DETERMINISTIC_MAP_RENAMED = _d(
    "plan_schema_deterministic_map_renamed", "Plan schema deterministic map renamed."
)
POOL_CAPACITY_PRE_FLIGHT_UNIQUE = _d(
    "pool_capacity_pre_flight_unique", "Pool capacity pre flight unique."
)
POOL_SIZE_LOCATION_CONFLICT = _d("pool_size_location_conflict", "Pool size location conflict.")
SEED_NOT_NUMERIC = _d("seed_not_numeric", "Seed not numeric.")
SEED_OVERFLOW = _d("seed_overflow", "Seed overflow.")
STATISTICAL_CONDITION_COLUMN_UNAVAILABLE = _d(
    "statistical_condition_column_unavailable", "Statistical condition column unavailable."
)
STATISTICAL_HIGH_CARDINALITY_WRONG_TYPE = _d(
    "statistical_high_cardinality_wrong_type", "Statistical high cardinality wrong type."
)
STATISTICAL_SNAPSHOT_FILE_REQUIRED = _d(
    "statistical_snapshot_file_required", "Statistical snapshot file required."
)
STATISTICAL_SNAPSHOT_NOT_PINNED = _d(
    "statistical_snapshot_not_pinned", "Statistical snapshot not pinned."
)
STRATEGY_NOT_CHUNK_SAFE = _d("strategy_not_chunk_safe", "Strategy not chunk safe.")
TOP_CODE_BOUNDS_UNRESOLVABLE = _d("top_code_bounds_unresolvable", "Top code bounds unresolvable.")
TOP_CODE_FLOOR_GE_CAP = _d("top_code_floor_ge_cap", "Top code floor ge cap.")
TOP_CODE_MISSING_OVER_LABEL = _d("top_code_missing_over_label", "Top code missing over label.")
TOP_CODE_MISSING_UNDER_LABEL = _d("top_code_missing_under_label", "Top code missing under label.")
TOP_CODE_UNDER_LABEL_WITHOUT_FLOOR = _d(
    "top_code_under_label_without_floor", "Top code under label without floor."
)
TOP_CODE_UNSUPPORTED_IN_NESTED = _d(
    "top_code_unsupported_in_nested", "Top code unsupported in nested."
)
TOP_CODE_WITH_WHEN_UNSUPPORTED = _d(
    "top_code_with_when_unsupported", "Top code with when unsupported."
)
TRUNCATE_KEEP_INVALID = _d("truncate_keep_invalid", "Truncate keep invalid.")
TRUNCATE_LENGTH_INVALID = _d("truncate_length_invalid", "Truncate length invalid.")
TRUNCATE_MASK_CHAR_INVALID = _d("truncate_mask_char_invalid", "Truncate mask char invalid.")
UNKNOWN_PROVIDER = _d("unknown_provider", "Unknown provider.")
VAULT_REQUIRES_CRYPTOGRAPHY = _d("vault_requires_cryptography", "Vault requires cryptography.")
VAULT_REQUIRES_NAMESPACE = _d("vault_requires_namespace", "Vault requires namespace.")
VAULT_STRATEGY_REVERSIBLE = _d("vault_strategy_reversible", "Vault strategy reversible.")
VAULT_STRATEGY_UNMASKED = _d("vault_strategy_unmasked", "Vault strategy unmasked.")
WHEN_OUTSIDE_CLOSED_GRAMMAR = _d("when_outside_closed_grammar", "When outside closed grammar.")
WHEN_STRING_OUTPUT_ON_NUMERIC_UNSUPPORTED = _d(
    "when_string_output_on_numeric_unsupported", "When string output on numeric unsupported."
)
WHEN_WITH_COHERENT_WITH_UNSUPPORTED = _d(
    "when_with_coherent_with_unsupported", "When with coherent with unsupported."
)
WINDOWED_DATE_ANCHOR_MISSING = _d("windowed_date_anchor_missing", "Windowed date anchor missing.")
WINDOWED_DATE_DISTRIBUTION_INVALID = _d(
    "windowed_date_distribution_invalid", "Windowed date distribution invalid."
)
WINDOWED_DATE_MAX_DAYS_MISSING = _d(
    "windowed_date_max_days_missing", "Windowed date max days missing."
)
WINDOWED_DATE_MIN_EXCEEDS_MAX = _d(
    "windowed_date_min_exceeds_max", "Windowed date min exceeds max."
)
WINDOWED_DATE_MISSING_ANCHOR_REF = _d(
    "windowed_date_missing_anchor_ref", "Windowed date missing anchor ref."
)

_d = _catalog.plan("StatisticalSpecError")

STATISTICAL_ALLOW_REAL_CATEGORIES_INVALID_TYPE = _d(
    "statistical_allow_real_categories_invalid_type",
    "Statistical allow real categories invalid type.",
)
STATISTICAL_COLUMN_NOT_IN_SNAPSHOT = _d(
    "statistical_column_not_in_snapshot", "Statistical column not in snapshot."
)
STATISTICAL_CONDITION_KIND_INVALID = _d(
    "statistical_condition_kind_invalid", "Statistical condition kind invalid."
)
STATISTICAL_DP_CARRIER_INVALID = _d(
    "statistical_dp_carrier_invalid", "Statistical DP carrier invalid."
)
STATISTICAL_FLAG_OTHER_MODE_EMIT_INVALID = _d(
    "statistical_flag_other_mode_emit_invalid", "Statistical flag other mode emit invalid."
)
STATISTICAL_FLAG_REQUIRES_DP_DECLARATION = _d(
    "statistical_flag_requires_dp_declaration", "Statistical flag requires DP declaration."
)
STATISTICAL_FLAG_TOKEN_INVALID = _d(
    "statistical_flag_token_invalid", "Statistical flag token invalid."
)
STATISTICAL_HIGH_CARDINALITY_INVALID_TYPE = _d(
    "statistical_high_cardinality_invalid_type", "Statistical high cardinality invalid type."
)
STATISTICAL_HIGH_CARDINALITY_KIND_INVALID = _d(
    "statistical_high_cardinality_kind_invalid", "Statistical high cardinality kind invalid."
)

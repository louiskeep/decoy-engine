"""Catalog of the ``plan`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "ALLOW_COLLISIONS_MODE_CONFLICT",
    "BUCKETIZE_WIDTH_UNRESOLVABLE",
    "BUCKET_PERTURB_DATE_FORMAT_UNSUPPORTED",
    "BUCKET_PERTURB_PRESERVE_VALUES_INVALID",
    "CATEGORICAL_CATEGORIES_MISSING",
    "CATEGORICAL_CATEGORIES_NOT_LIST",
    "CATEGORICAL_NONDETERMINISTIC_NOT_CHUNK_SAFE",
    "CHUNKED_BUCKET_PERTURB_FK_KEY_UNSUPPORTED",
    "CHUNKED_BUCKET_PERTURB_SOURCE_DTYPE_UNSUPPORTED",
    "CHUNKED_BUCKET_PERTURB_WHEN_NOT_SUPPORTED",
    "CHUNKED_CATEGORICAL_NONDETERMINISTIC_WHEN_NOT_SUPPORTED",
    "CHUNKED_CODE_SET_FK_KEY_UNSUPPORTED",
    "CHUNKED_CODE_SET_SOURCE_DTYPE_UNSUPPORTED",
    "CHUNKED_CODE_SET_WHEN_NOT_SUPPORTED",
    "CHUNKED_FAKER_NONDETERMINISTIC_WHEN_NOT_SUPPORTED",
    "CHUNKED_FK_CHILD_CONFIG_MISMATCH",
    "CHUNKED_FK_CHILD_KEY_DTYPE_MISMATCH",
    "CHUNKED_FK_CHILD_KEY_DTYPE_UNPROVABLE",
    "CHUNKED_FK_CHILD_NAMESPACE_MISMATCH",
    "CHUNKED_FK_CHILD_NAMESPACE_MISSING",
    "CHUNKED_FK_CHILD_STRATEGY_MISMATCH",
    "CHUNKED_FK_CHILD_STRATEGY_MISSING",
    "CHUNKED_FK_CHILD_WHEN_UNSUPPORTED",
    "CHUNKED_FK_COMPOSITE_UNSUPPORTED",
    "CHUNKED_FK_ENDPOINT_NOT_SCALAR",
    "CHUNKED_FK_KEY_DTYPE_NOT_CROSS_ADAPTER_SAFE",
    "CHUNKED_FK_ORPHAN_POLICY_NOT_REMAP",
    "CHUNKED_FK_PARENT_NAMESPACE_MISMATCH",
    "CHUNKED_FK_PARENT_NAMESPACE_MISSING",
    "CHUNKED_FK_PARENT_NOT_ROOT",
    "CHUNKED_FK_PARENT_STRATEGY_NOT_SELF_MASK_SAFE",
    "CHUNKED_FK_PARENT_WHEN_UNSUPPORTED",
    "CHUNKED_GENERATE_UNSUPPORTED",
    "CHUNKED_GROUP_KEY_GROUP_BY_DTYPE_UNSUPPORTED",
    "CHUNKED_GROUP_KEY_WHEN_NOT_SUPPORTED",
    "CHUNKED_STRATEGY_CONDITIONS_UNMET",
    "CHUNKED_TABLE_UNKNOWN",
    "CHUNKED_TEXT_MASK_SOURCE_DTYPE_UNSUPPORTED",
    "CHUNKED_TEXT_MASK_WHEN_NOT_SUPPORTED",
    "CHUNKED_WINDOWED_DATE_WHEN_NOT_SUPPORTED",
    "CODE_SET_CHAPTER_ABSENT",
    "CODE_SET_CHAPTER_COLUMN_MISSING",
    "CODE_SET_CORPUS_DUPLICATE_CODES",
    "CODE_SET_CORPUS_EMPTY",
    "CODE_SET_CORPUS_EMPTY_CODE",
    "CODE_SET_CORPUS_INCOHERENT_CHAPTER",
    "CODE_SET_CORPUS_MISSING_CODE_COLUMN",
    "CODE_SET_CORPUS_MISSING_PROVENANCE",
    "CODE_SET_CORPUS_NOT_FOUND",
    "CODE_SET_CORPUS_NULL_CODE",
    "CODE_SET_CORPUS_PATH_NOT_FOUND",
    "CODE_SET_CORPUS_PROVENANCE_IDENTITY_MISMATCH",
    "CODE_SET_CORPUS_PROVENANCE_MALFORMED_STAMP",
    "CODE_SET_CORPUS_READ_ERROR",
    "CODE_SET_CORPUS_SOURCE_VERSION_INVALID",
    "CODE_SET_CORPUS_VERSION_MISMATCH",
    "CODE_SET_GEN_REQUIRES_NAMESPACE",
    "CODE_SET_NAME_MISSING",
    "CODE_SET_RESERVED_LICENSED_NAME",
    "CODE_SET_SINGLE_ROW_CORPUS",
    "CODE_SET_SOLE_MEMBER_BUCKET",
    "COMPOSITE_COLUMNS_LENGTH_MISMATCH",
    "COMPOSITE_CUSTOM_BUNDLE_ITEM_MISSING_KEYS",
    "COMPOSITE_CUSTOM_BUNDLE_ITEM_SHAPE",
    "COMPOSITE_CUSTOM_BUNDLE_SIZE",
    "COMPOSITE_CUSTOM_DUPLICATE_COLUMNS",
    "COMPOSITE_CUSTOM_NO_NESTING",
    "COMPOSITE_REQUIRES_BUNDLE_PATH",
    "COMPOSITE_WIRING_INCONSISTENT",
    "DATE_PRESERVE_VALUES_UNSUPPORTED_FK_SHAPE",
    "DATE_PRESERVE_VALUES_WITH_GROUP_BY_UNSUPPORTED",
    "DATE_SHIFT_GROUP_BY_ON_FK_COLUMN_UNSUPPORTED",
    "DATE_SHIFT_GROUP_BY_UNSUPPORTED_IN_NESTED",
    "DATE_SHIFT_GROUP_BY_WITH_WHEN_UNSUPPORTED",
    "DATE_SHIFT_MISSING_GROUP_BY_REF",
    "DATE_SHIFT_PRESERVE_VALUES_INVALID",
    "DERIVED_AGGREGATE_COLUMN_MISSING",
    "DERIVED_AGGREGATE_MISSING_COLUMN_REF",
    "DERIVED_AGGREGATE_OP_INVALID",
    "DERIVED_AGGREGATE_OP_MISSING",
    "DERIVED_BOUNDS_INVALID",
    "DETERMINISTIC_REQUIRES_SOURCE_AND_SEED",
    "EMPTY_DOMAIN_POOL",
    "EMPTY_LOCALITY_TABLE",
    "GENERATION_NOT_CLASS_A",
    "GENERATION_UNBOUNDED_COLUMN",
    "PINNED_DETERMINISM_FRESH",
    "PINNED_LIVE_TOKEN",
    "PINNED_NOT_GENERATION_ONLY",
    "PINNED_PLAN_MALFORMED",
    "PINNED_REFERENCE_DATE_INVALID",
    "PINNED_RUNTIME_CHANGED",
    "PINNED_SEED_INVALID",
    "PINNED_SNAPSHOT_FILE_NOT_STATISTICAL",
    "PINNED_STATISTICAL_DIGEST_MISSING",
    "PINNED_TYPE_NOT_ALLOWLISTED",
    "PINNED_WINDOWED_ANCHOR_NOT_ABSOLUTE",
    "POOL_SIZE_OVERFLOW",
    "SOURCE_LENGTH_MISMATCH",
    "UNKNOWN_COMPOSITE",
]

_d = _catalog.plan("CompositeError")

COMPOSITE_CUSTOM_BUNDLE_ITEM_MISSING_KEYS = _d(
    "composite_custom_bundle_item_missing_keys", "Composite custom bundle item missing keys."
)
COMPOSITE_CUSTOM_BUNDLE_ITEM_SHAPE = _d(
    "composite_custom_bundle_item_shape", "Composite custom bundle item shape."
)
COMPOSITE_CUSTOM_BUNDLE_SIZE = _d("composite_custom_bundle_size", "Composite custom bundle size.")
COMPOSITE_CUSTOM_DUPLICATE_COLUMNS = _d(
    "composite_custom_duplicate_columns", "Composite custom duplicate columns."
)
COMPOSITE_CUSTOM_NO_NESTING = _d("composite_custom_no_nesting", "Composite custom no nesting.")
COMPOSITE_REQUIRES_BUNDLE_PATH = _d(
    "composite_requires_bundle_path", "Composite requires bundle path."
)
DETERMINISTIC_REQUIRES_SOURCE_AND_SEED = _d(
    "deterministic_requires_source_and_seed", "Deterministic requires source and seed."
)
EMPTY_DOMAIN_POOL = _d("empty_domain_pool", "Empty domain pool.")
EMPTY_LOCALITY_TABLE = _d("empty_locality_table", "Empty locality table.")
POOL_SIZE_OVERFLOW = _d("pool_size_overflow", "Pool size overflow.")
SOURCE_LENGTH_MISMATCH = _d("source_length_mismatch", "Source length mismatch.")
UNKNOWN_COMPOSITE = _d("unknown_composite", "Unknown composite.")

_d = _catalog.plan("GenerationAdmissionError")

GENERATION_NOT_CLASS_A = _d("generation_not_class_a", "Generation not class a.")
GENERATION_UNBOUNDED_COLUMN = _d("generation_unbounded_column", "Generation unbounded column.")

_d = _catalog.plan("PinnedModeError")

PINNED_DETERMINISM_FRESH = _d("pinned_determinism_fresh", "Pinned determinism fresh.")
PINNED_LIVE_TOKEN = _d("pinned_live_token", "Pinned live token.")
PINNED_NOT_GENERATION_ONLY = _d("pinned_not_generation_only", "Pinned not generation only.")
PINNED_PLAN_MALFORMED = _d("pinned_plan_malformed", "Pinned plan malformed.")
PINNED_REFERENCE_DATE_INVALID = _d(
    "pinned_reference_date_invalid", "Pinned reference date invalid."
)
PINNED_RUNTIME_CHANGED = _d("pinned_runtime_changed", "Pinned runtime changed.")
PINNED_SEED_INVALID = _d("pinned_seed_invalid", "Pinned seed invalid.")
PINNED_SNAPSHOT_FILE_NOT_STATISTICAL = _d(
    "pinned_snapshot_file_not_statistical", "Pinned snapshot file not statistical."
)
PINNED_STATISTICAL_DIGEST_MISSING = _d(
    "pinned_statistical_digest_missing", "Pinned statistical digest missing."
)
PINNED_TYPE_NOT_ALLOWLISTED = _d("pinned_type_not_allowlisted", "Pinned type not allowlisted.")
PINNED_WINDOWED_ANCHOR_NOT_ABSOLUTE = _d(
    "pinned_windowed_anchor_not_absolute", "Pinned windowed anchor not absolute."
)

_d = _catalog.plan("PlanCompileError")

ALLOW_COLLISIONS_MODE_CONFLICT = _d(
    "allow_collisions_mode_conflict", "Allow collisions mode conflict."
)
BUCKET_PERTURB_DATE_FORMAT_UNSUPPORTED = _d(
    "bucket_perturb_date_format_unsupported", "Bucket perturb date format unsupported."
)
BUCKET_PERTURB_PRESERVE_VALUES_INVALID = _d(
    "bucket_perturb_preserve_values_invalid", "Bucket perturb preserve values invalid."
)
BUCKETIZE_WIDTH_UNRESOLVABLE = _d("bucketize_width_unresolvable", "Bucketize width unresolvable.")
CATEGORICAL_CATEGORIES_MISSING = _d(
    "categorical_categories_missing", "Categorical categories missing."
)
CATEGORICAL_CATEGORIES_NOT_LIST = _d(
    "categorical_categories_not_list", "Categorical categories not list."
)
CATEGORICAL_NONDETERMINISTIC_NOT_CHUNK_SAFE = _d(
    "categorical_nondeterministic_not_chunk_safe", "Categorical nondeterministic not chunk safe."
)
CHUNKED_BUCKET_PERTURB_FK_KEY_UNSUPPORTED = _d(
    "chunked_bucket_perturb_fk_key_unsupported", "Chunked bucket perturb FK key unsupported."
)
CHUNKED_BUCKET_PERTURB_SOURCE_DTYPE_UNSUPPORTED = _d(
    "chunked_bucket_perturb_source_dtype_unsupported",
    "Chunked bucket perturb source dtype unsupported.",
)
CHUNKED_BUCKET_PERTURB_WHEN_NOT_SUPPORTED = _d(
    "chunked_bucket_perturb_when_not_supported", "Chunked bucket perturb when not supported."
)
CHUNKED_CATEGORICAL_NONDETERMINISTIC_WHEN_NOT_SUPPORTED = _d(
    "chunked_categorical_nondeterministic_when_not_supported",
    "Chunked categorical nondeterministic when not supported.",
)
CHUNKED_CODE_SET_FK_KEY_UNSUPPORTED = _d(
    "chunked_code_set_fk_key_unsupported", "Chunked code set FK key unsupported."
)
CHUNKED_CODE_SET_SOURCE_DTYPE_UNSUPPORTED = _d(
    "chunked_code_set_source_dtype_unsupported", "Chunked code set source dtype unsupported."
)
CHUNKED_CODE_SET_WHEN_NOT_SUPPORTED = _d(
    "chunked_code_set_when_not_supported", "Chunked code set when not supported."
)
CHUNKED_FAKER_NONDETERMINISTIC_WHEN_NOT_SUPPORTED = _d(
    "chunked_faker_nondeterministic_when_not_supported",
    "Chunked Faker nondeterministic when not supported.",
)
CHUNKED_FK_CHILD_CONFIG_MISMATCH = _d(
    "chunked_fk_child_config_mismatch", "Chunked FK child config mismatch."
)
CHUNKED_FK_CHILD_KEY_DTYPE_MISMATCH = _d(
    "chunked_fk_child_key_dtype_mismatch", "Chunked FK child key dtype mismatch."
)
CHUNKED_FK_CHILD_KEY_DTYPE_UNPROVABLE = _d(
    "chunked_fk_child_key_dtype_unprovable", "Chunked FK child key dtype unprovable."
)
CHUNKED_FK_CHILD_NAMESPACE_MISMATCH = _d(
    "chunked_fk_child_namespace_mismatch", "Chunked FK child namespace mismatch."
)
CHUNKED_FK_CHILD_NAMESPACE_MISSING = _d(
    "chunked_fk_child_namespace_missing", "Chunked FK child namespace missing."
)
CHUNKED_FK_CHILD_STRATEGY_MISMATCH = _d(
    "chunked_fk_child_strategy_mismatch", "Chunked FK child strategy mismatch."
)
CHUNKED_FK_CHILD_STRATEGY_MISSING = _d(
    "chunked_fk_child_strategy_missing", "Chunked FK child strategy missing."
)
CHUNKED_FK_CHILD_WHEN_UNSUPPORTED = _d(
    "chunked_fk_child_when_unsupported", "Chunked FK child when unsupported."
)
CHUNKED_FK_COMPOSITE_UNSUPPORTED = _d(
    "chunked_fk_composite_unsupported", "Chunked FK composite unsupported."
)
CHUNKED_FK_ENDPOINT_NOT_SCALAR = _d(
    "chunked_fk_endpoint_not_scalar", "Chunked FK endpoint not scalar."
)
CHUNKED_FK_KEY_DTYPE_NOT_CROSS_ADAPTER_SAFE = _d(
    "chunked_fk_key_dtype_not_cross_adapter_safe", "Chunked FK key dtype not cross adapter safe."
)
CHUNKED_FK_ORPHAN_POLICY_NOT_REMAP = _d(
    "chunked_fk_orphan_policy_not_remap", "Chunked FK orphan policy not remap."
)
CHUNKED_FK_PARENT_NAMESPACE_MISMATCH = _d(
    "chunked_fk_parent_namespace_mismatch", "Chunked FK parent namespace mismatch."
)
CHUNKED_FK_PARENT_NAMESPACE_MISSING = _d(
    "chunked_fk_parent_namespace_missing", "Chunked FK parent namespace missing."
)
CHUNKED_FK_PARENT_NOT_ROOT = _d("chunked_fk_parent_not_root", "Chunked FK parent not root.")
CHUNKED_FK_PARENT_STRATEGY_NOT_SELF_MASK_SAFE = _d(
    "chunked_fk_parent_strategy_not_self_mask_safe",
    "Chunked FK parent strategy not self mask safe.",
)
CHUNKED_FK_PARENT_WHEN_UNSUPPORTED = _d(
    "chunked_fk_parent_when_unsupported", "Chunked FK parent when unsupported."
)
CHUNKED_GENERATE_UNSUPPORTED = _d("chunked_generate_unsupported", "Chunked generate unsupported.")
CHUNKED_GROUP_KEY_GROUP_BY_DTYPE_UNSUPPORTED = _d(
    "chunked_group_key_group_by_dtype_unsupported", "Chunked group key group by dtype unsupported."
)
CHUNKED_GROUP_KEY_WHEN_NOT_SUPPORTED = _d(
    "chunked_group_key_when_not_supported", "Chunked group key when not supported."
)
CHUNKED_STRATEGY_CONDITIONS_UNMET = _d(
    "chunked_strategy_conditions_unmet", "Chunked strategy conditions unmet."
)
CHUNKED_TABLE_UNKNOWN = _d("chunked_table_unknown", "Chunked table unknown.")
CHUNKED_TEXT_MASK_SOURCE_DTYPE_UNSUPPORTED = _d(
    "chunked_text_mask_source_dtype_unsupported", "Chunked text mask source dtype unsupported."
)
CHUNKED_TEXT_MASK_WHEN_NOT_SUPPORTED = _d(
    "chunked_text_mask_when_not_supported", "Chunked text mask when not supported."
)
CHUNKED_WINDOWED_DATE_WHEN_NOT_SUPPORTED = _d(
    "chunked_windowed_date_when_not_supported", "Chunked windowed date when not supported."
)
CODE_SET_CHAPTER_ABSENT = _d("code_set_chapter_absent", "Code set chapter absent.")
CODE_SET_CHAPTER_COLUMN_MISSING = _d(
    "code_set_chapter_column_missing", "Code set chapter column missing."
)
CODE_SET_CORPUS_DUPLICATE_CODES = _d(
    "code_set_corpus_duplicate_codes", "Code set corpus duplicate codes."
)
CODE_SET_CORPUS_EMPTY = _d("code_set_corpus_empty", "Code set corpus empty.")
CODE_SET_CORPUS_EMPTY_CODE = _d("code_set_corpus_empty_code", "Code set corpus empty code.")
CODE_SET_CORPUS_INCOHERENT_CHAPTER = _d(
    "code_set_corpus_incoherent_chapter", "Code set corpus incoherent chapter."
)
CODE_SET_CORPUS_MISSING_CODE_COLUMN = _d(
    "code_set_corpus_missing_code_column", "Code set corpus missing code column."
)
CODE_SET_CORPUS_MISSING_PROVENANCE = _d(
    "code_set_corpus_missing_provenance", "Code set corpus missing provenance."
)
CODE_SET_CORPUS_NOT_FOUND = _d("code_set_corpus_not_found", "Code set corpus not found.")
CODE_SET_CORPUS_NULL_CODE = _d("code_set_corpus_null_code", "Code set corpus null code.")
CODE_SET_CORPUS_PATH_NOT_FOUND = _d(
    "code_set_corpus_path_not_found", "Code set corpus path not found."
)
CODE_SET_CORPUS_PROVENANCE_IDENTITY_MISMATCH = _d(
    "code_set_corpus_provenance_identity_mismatch", "Code set corpus provenance identity mismatch."
)
CODE_SET_CORPUS_PROVENANCE_MALFORMED_STAMP = _d(
    "code_set_corpus_provenance_malformed_stamp", "Code set corpus provenance malformed stamp."
)
CODE_SET_CORPUS_READ_ERROR = _d("code_set_corpus_read_error", "Code set corpus read error.")
CODE_SET_CORPUS_SOURCE_VERSION_INVALID = _d(
    "code_set_corpus_source_version_invalid", "Code set corpus source version invalid."
)
CODE_SET_CORPUS_VERSION_MISMATCH = _d(
    "code_set_corpus_version_mismatch", "Code set corpus version mismatch."
)
CODE_SET_GEN_REQUIRES_NAMESPACE = _d(
    "code_set_gen_requires_namespace", "Code set gen requires namespace."
)
CODE_SET_NAME_MISSING = _d("code_set_name_missing", "Code set name missing.")
CODE_SET_RESERVED_LICENSED_NAME = _d(
    "code_set_reserved_licensed_name", "Code set reserved licensed name."
)
CODE_SET_SINGLE_ROW_CORPUS = _d("code_set_single_row_corpus", "Code set single row corpus.")
CODE_SET_SOLE_MEMBER_BUCKET = _d("code_set_sole_member_bucket", "Code set sole member bucket.")
COMPOSITE_COLUMNS_LENGTH_MISMATCH = _d(
    "composite_columns_length_mismatch", "Composite columns length mismatch."
)
COMPOSITE_WIRING_INCONSISTENT = _d(
    "composite_wiring_inconsistent", "Composite wiring inconsistent."
)
DATE_PRESERVE_VALUES_UNSUPPORTED_FK_SHAPE = _d(
    "date_preserve_values_unsupported_fk_shape", "Date preserve values unsupported FK shape."
)
DATE_PRESERVE_VALUES_WITH_GROUP_BY_UNSUPPORTED = _d(
    "date_preserve_values_with_group_by_unsupported",
    "Date preserve values with group by unsupported.",
)
DATE_SHIFT_GROUP_BY_ON_FK_COLUMN_UNSUPPORTED = _d(
    "date_shift_group_by_on_fk_column_unsupported", "Date shift group by on FK column unsupported."
)
DATE_SHIFT_GROUP_BY_UNSUPPORTED_IN_NESTED = _d(
    "date_shift_group_by_unsupported_in_nested", "Date shift group by unsupported in nested."
)
DATE_SHIFT_GROUP_BY_WITH_WHEN_UNSUPPORTED = _d(
    "date_shift_group_by_with_when_unsupported", "Date shift group by with when unsupported."
)
DATE_SHIFT_MISSING_GROUP_BY_REF = _d(
    "date_shift_missing_group_by_ref", "Date shift missing group by ref."
)
DATE_SHIFT_PRESERVE_VALUES_INVALID = _d(
    "date_shift_preserve_values_invalid", "Date shift preserve values invalid."
)
DERIVED_AGGREGATE_COLUMN_MISSING = _d(
    "derived_aggregate_column_missing", "Derived aggregate column missing."
)
DERIVED_AGGREGATE_MISSING_COLUMN_REF = _d(
    "derived_aggregate_missing_column_ref", "Derived aggregate missing column ref."
)
DERIVED_AGGREGATE_OP_INVALID = _d("derived_aggregate_op_invalid", "Derived aggregate op invalid.")
DERIVED_AGGREGATE_OP_MISSING = _d("derived_aggregate_op_missing", "Derived aggregate op missing.")
DERIVED_BOUNDS_INVALID = _d("derived_bounds_invalid", "Derived bounds invalid.")

"""Catalog of the ``runtime`` namespace, part 3."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Outcome, Phase, RetryClass

__all__ = [
    "CATEGORICAL_POSITION_OUT_OF_DOMAIN",
    "CATEGORICAL_REQUIRES_CATEGORIES",
    "CATEGORICAL_REQUIRES_NAMESPACE",
    "CATEGORICAL_WEIGHTS_NEGATIVE",
    "CATEGORICAL_WEIGHTS_NONPOSITIVE",
    "CATEGORICAL_WEIGHTS_SHAPE",
    "CATEGORICAL_WEIGHT_BELOW_RESOLUTION",
    "CHECKSUM_INVALID",
    "CODE_SET_GEN_REQUIRES_NAMESPACE",
    "CONFIG_REFERENCES_STORED_INDEX",
    "DATE_PRESERVE_VALUES_WITH_GROUP_BY_UNSUPPORTED",
    "DATE_SHIFT_GROUP_ANCHOR_SNAPSHOT_MISALIGNED",
    "DATE_SHIFT_GROUP_ANCHOR_SNAPSHOT_MISSING",
    "DATE_SHIFT_INVALID_CONFIG",
    "DATE_SHIFT_REQUIRES_NAMESPACE",
    "DEDUPE_COLUMN_MISSING",
    "DEDUPE_UNSUPPORTED_TYPE",
    "DERIVE_COLUMN_ALREADY_EXISTS",
    "DERIVE_EXPRESSION_ERROR",
    "DERIVE_RESULT_NOT_CONVERTIBLE",
    "DROP_COLUMN_MISSING",
    "DUPLICATE_SOURCE_FIELD_NAMES",
    "FAKER_POSITIONAL_TABLE_UNKNOWN",
    "FAKER_POSITION_OUT_OF_DOMAIN",
    "FILTER_EXPRESSION_ERROR",
    "FILTER_EXPRESSION_NOT_BOOLEAN",
    "FPE_UNENCRYPTABLE_DOMAIN",
    "GENERATION_NOT_REPRODUCIBLE",
    "JOINT_MASK_KEY_BY_SNAPSHOT_MISALIGNED",
    "JOINT_MASK_KEY_BY_SNAPSHOT_MISSING",
    "NER_MODEL_VERSION_MISMATCH",
    "NESTED_CHILD_ROW_ERROR_UNMAPPABLE",
    "NESTED_CHILD_STRATEGY_UNKNOWN",
    "NESTED_JSONPATH_PARSE_ERROR",
    "NESTED_RECURSIVE_NESTED_REJECTED",
    "NESTED_STRATEGY_UNSET",
    "NESTED_TARGET_MATCHED_NOTHING",
    "NESTED_TARGET_UNSET",
    "NESTED_TARGET_UNWRITABLE_CONTAINER",
    "NUMEXPR_REQUIRED",
    "SHUFFLE_REQUIRES_NAMESPACE",
    "SORT_ASCENDING_LENGTH_MISMATCH",
    "SORT_COLUMN_MISSING",
    "SORT_UNSUPPORTED_TYPE",
    "SUBSET_CLOSURE_INVARIANT_VIOLATED",
    "SUBSET_CLOSURE_NONTERMINATION",
    "SUBSET_MATERIALIZE_COUNT_MISMATCH",
    "SUB_MINIMUM_DOMAIN",
    "TOKENIZE_REQUIRES_NAMESPACE",
    "TOKENIZE_REQUIRES_STRING_DTYPE",
    "TOKENIZE_REQUIRES_VAULT",
    "TOKENIZE_VAULT_ERROR",
    "TOP_CODE_BOUNDS_UNRESOLVABLE",
    "TOP_CODE_NON_INTEGRAL_OBJECT_COLUMN",
    "TOP_CODE_WITH_WHEN_UNSUPPORTED",
    "TRANSFORM_LINEAGE_MISMATCH",
    "TRUNCATE_KEEP_INVALID",
    "TRUNCATE_LENGTH_INVALID",
    "TRUNCATE_MASK_CHAR_INVALID",
    "UNIFIED_SLICE_INVARIANT_VIOLATION",
    "UNKNOWN_TRANSFORM_OP",
    "VAULT_CRYPTO_NOT_INSTALLED",
    "VAULT_FORMAT_UNSUPPORTED",
    "VAULT_KEY_MISMATCH",
    "VAULT_PROTOCOL_VERSION_MISMATCH",
    "VAULT_UNREADABLE",
    "VAULT_WRITER_UNSUPPORTED",
    "WEAK_MASK_SECRET",
    "WHEN_EXPRESSION_ERROR",
    "WHEN_EXPRESSION_NOT_BOOLEAN",
    "WHEN_OUTSIDE_CLOSED_GRAMMAR",
    "WHEN_PREDICATE_NOT_NATIVE",
    "WHEN_PREDICATE_OUTSIDE_NATIVE_SUBSET",
    "WHEN_PREDICATE_READS_MASKED_COLUMN",
    "WINDOWED_DATE_POSITION_OUT_OF_DOMAIN",
]

_d = _catalog.runtime("StrategyError")

CATEGORICAL_REQUIRES_CATEGORIES = _d(
    "categorical_requires_categories", "Categorical requires categories."
)
CATEGORICAL_REQUIRES_NAMESPACE = _d(
    "categorical_requires_namespace", "Categorical requires namespace."
)
CATEGORICAL_WEIGHT_BELOW_RESOLUTION = _d(
    "categorical_weight_below_resolution", "Categorical weight below resolution."
)
CATEGORICAL_WEIGHTS_NEGATIVE = _d("categorical_weights_negative", "Categorical weights negative.")
CATEGORICAL_WEIGHTS_NONPOSITIVE = _d(
    "categorical_weights_nonpositive", "Categorical weights nonpositive."
)
CATEGORICAL_WEIGHTS_SHAPE = _d("categorical_weights_shape", "Categorical weights shape.")
CODE_SET_GEN_REQUIRES_NAMESPACE = _d(
    "code_set_gen_requires_namespace", "Code set gen requires namespace."
)
DATE_PRESERVE_VALUES_WITH_GROUP_BY_UNSUPPORTED = _d(
    "date_preserve_values_with_group_by_unsupported",
    "Date preserve values with group by unsupported.",
)
DATE_SHIFT_GROUP_ANCHOR_SNAPSHOT_MISALIGNED = _d(
    "date_shift_group_anchor_snapshot_misaligned", "Date shift group anchor snapshot misaligned."
)
DATE_SHIFT_GROUP_ANCHOR_SNAPSHOT_MISSING = _d(
    "date_shift_group_anchor_snapshot_missing", "Date shift group anchor snapshot missing."
)
DATE_SHIFT_INVALID_CONFIG = _d("date_shift_invalid_config", "Date shift invalid config.")
DATE_SHIFT_REQUIRES_NAMESPACE = _d(
    "date_shift_requires_namespace", "Date shift requires namespace."
)
FAKER_POSITIONAL_TABLE_UNKNOWN = _d(
    "faker_positional_table_unknown", "Faker positional table unknown."
)
FPE_UNENCRYPTABLE_DOMAIN = _d("fpe_unencryptable_domain", "FPE unencryptable domain.")
JOINT_MASK_KEY_BY_SNAPSHOT_MISALIGNED = _d(
    "joint_mask_key_by_snapshot_misaligned", "Joint mask key by snapshot misaligned."
)
JOINT_MASK_KEY_BY_SNAPSHOT_MISSING = _d(
    "joint_mask_key_by_snapshot_missing", "Joint mask key by snapshot missing."
)
NER_MODEL_VERSION_MISMATCH = _d("ner_model_version_mismatch", "NER model version mismatch.")
NESTED_CHILD_ROW_ERROR_UNMAPPABLE = _d(
    "nested_child_row_error_unmappable", "Nested child row error unmappable."
)
NESTED_CHILD_STRATEGY_UNKNOWN = _d(
    "nested_child_strategy_unknown", "Nested child strategy unknown."
)
NESTED_JSONPATH_PARSE_ERROR = _d("nested_jsonpath_parse_error", "Nested jsonpath parse error.")
NESTED_RECURSIVE_NESTED_REJECTED = _d(
    "nested_recursive_nested_rejected", "Nested recursive nested rejected."
)
NESTED_STRATEGY_UNSET = _d("nested_strategy_unset", "Nested strategy unset.")
NESTED_TARGET_MATCHED_NOTHING = _d(
    "nested_target_matched_nothing", "Nested target matched nothing."
)
NESTED_TARGET_UNSET = _d("nested_target_unset", "Nested target unset.")
NESTED_TARGET_UNWRITABLE_CONTAINER = _d(
    "nested_target_unwritable_container", "Nested target unwritable container."
)
NUMEXPR_REQUIRED = _d("numexpr_required", "Numexpr required.")
SHUFFLE_REQUIRES_NAMESPACE = _d("shuffle_requires_namespace", "Shuffle requires namespace.")
TOKENIZE_REQUIRES_NAMESPACE = _d("tokenize_requires_namespace", "Tokenize requires namespace.")
TOKENIZE_REQUIRES_STRING_DTYPE = _d(
    "tokenize_requires_string_dtype", "Tokenize requires string dtype."
)
TOKENIZE_REQUIRES_VAULT = _d("tokenize_requires_vault", "Tokenize requires vault.")
TOKENIZE_VAULT_ERROR = _d("tokenize_vault_error", "Tokenize vault error.")
TOP_CODE_BOUNDS_UNRESOLVABLE = _d("top_code_bounds_unresolvable", "Top code bounds unresolvable.")
TOP_CODE_NON_INTEGRAL_OBJECT_COLUMN = _d(
    "top_code_non_integral_object_column", "Top code non integral object column."
)
TOP_CODE_WITH_WHEN_UNSUPPORTED = _d(
    "top_code_with_when_unsupported", "Top code with when unsupported."
)
TRUNCATE_KEEP_INVALID = _d("truncate_keep_invalid", "Truncate keep invalid.")
TRUNCATE_LENGTH_INVALID = _d("truncate_length_invalid", "Truncate length invalid.")
TRUNCATE_MASK_CHAR_INVALID = _d("truncate_mask_char_invalid", "Truncate mask char invalid.")
WHEN_EXPRESSION_ERROR = _d("when_expression_error", "When expression error.")
WHEN_EXPRESSION_NOT_BOOLEAN = _d("when_expression_not_boolean", "When expression not boolean.")
WHEN_OUTSIDE_CLOSED_GRAMMAR = _d("when_outside_closed_grammar", "When outside closed grammar.")

_d = _catalog.runtime("SubsetInternalError")

SUBSET_CLOSURE_INVARIANT_VIOLATED = _d(
    "subset_closure_invariant_violated", "Subset closure invariant violated."
)
SUBSET_CLOSURE_NONTERMINATION = _d(
    "subset_closure_nontermination", "Subset closure nontermination."
)
SUBSET_MATERIALIZE_COUNT_MISMATCH = _d(
    "subset_materialize_count_mismatch", "Subset materialize count mismatch."
)

_d = _catalog.runtime("TransformError")

CONFIG_REFERENCES_STORED_INDEX = _d(
    "config_references_stored_index", "Config references stored index."
)
DEDUPE_COLUMN_MISSING = _d("dedupe_column_missing", "Dedupe column missing.")
DEDUPE_UNSUPPORTED_TYPE = _d("dedupe_unsupported_type", "Dedupe unsupported type.")
DERIVE_COLUMN_ALREADY_EXISTS = _d("derive_column_already_exists", "Derive column already exists.")
DERIVE_EXPRESSION_ERROR = _d("derive_expression_error", "Derive expression error.")
DROP_COLUMN_MISSING = _d("drop_column_missing", "Drop column missing.")
DUPLICATE_SOURCE_FIELD_NAMES = _d("duplicate_source_field_names", "Duplicate source field names.")
FILTER_EXPRESSION_ERROR = _d("filter_expression_error", "Filter expression error.")
FILTER_EXPRESSION_NOT_BOOLEAN = _d(
    "filter_expression_not_boolean", "Filter expression not boolean."
)
SORT_ASCENDING_LENGTH_MISMATCH = _d(
    "sort_ascending_length_mismatch", "Sort ascending length mismatch."
)
SORT_COLUMN_MISSING = _d("sort_column_missing", "Sort column missing.")
SORT_UNSUPPORTED_TYPE = _d("sort_unsupported_type", "Sort unsupported type.")
UNKNOWN_TRANSFORM_OP = _d("unknown_transform_op", "Unknown transform op.")

_d = _catalog.runtime("UnifiedSliceInvariantError")

UNIFIED_SLICE_INVARIANT_VIOLATION = _d(
    "unified_slice_invariant_violation", "Unified slice invariant violation."
)

_d = _catalog.runtime("VaultError")

VAULT_CRYPTO_NOT_INSTALLED = _d(
    "vault_crypto_not_installed",
    "Vault crypto not installed.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
VAULT_FORMAT_UNSUPPORTED = _d(
    "vault_format_unsupported",
    "Vault format unsupported.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
VAULT_KEY_MISMATCH = _d(
    "vault_key_mismatch",
    "Vault key mismatch.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
VAULT_PROTOCOL_VERSION_MISMATCH = _d(
    "vault_protocol_version_mismatch",
    "Vault protocol version mismatch.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
VAULT_UNREADABLE = _d(
    "vault_unreadable",
    "Vault unreadable.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)
VAULT_WRITER_UNSUPPORTED = _d(
    "vault_writer_unsupported",
    "Vault writer unsupported.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)

_d = _catalog.runtime("WeakMaskSecret")

WEAK_MASK_SECRET = _d(
    "weak_mask_secret",
    "Weak mask secret.",
    phases=(Phase.RESOLVE,),
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
)

_d = _catalog.runtime("decoy_engine.execution._strategies._categorical")

CATEGORICAL_POSITION_OUT_OF_DOMAIN = _d(
    "categorical_position_out_of_domain", "Categorical position out of domain."
)

_d = _catalog.runtime("decoy_engine.execution._strategies._faker_positional")

FAKER_POSITION_OUT_OF_DOMAIN = _d("faker_position_out_of_domain", "Faker position out of domain.")

_d = _catalog.runtime("decoy_engine.execution._strategies._windowed_date")

WINDOWED_DATE_POSITION_OUT_OF_DOMAIN = _d(
    "windowed_date_position_out_of_domain", "Windowed date position out of domain."
)

_d = _catalog.runtime("decoy_engine.execution._transforms_table")

DERIVE_RESULT_NOT_CONVERTIBLE = _d(
    "derive_result_not_convertible", "Derive result not convertible."
)
TRANSFORM_LINEAGE_MISMATCH = _d("transform_lineage_mismatch", "Transform lineage mismatch.")

_d = _catalog.runtime("decoy_engine.execution.native._when_admission")

WHEN_PREDICATE_NOT_NATIVE = _d("when_predicate_not_native", "When predicate not native.")
WHEN_PREDICATE_OUTSIDE_NATIVE_SUBSET = _d(
    "when_predicate_outside_native_subset", "When predicate outside native subset."
)
WHEN_PREDICATE_READS_MASKED_COLUMN = _d(
    "when_predicate_reads_masked_column", "When predicate reads masked column."
)

_d = _catalog.runtime("decoy_engine.generation._window_admission")

GENERATION_NOT_REPRODUCIBLE = _d("generation_not_reproducible", "Generation not reproducible.")

_d = _catalog.runtime("decoy_engine.transforms.text_mask")

CHECKSUM_INVALID = _d("checksum_invalid", "Checksum invalid.")
SUB_MINIMUM_DOMAIN = _d("sub_minimum_domain", "Sub minimum domain.")

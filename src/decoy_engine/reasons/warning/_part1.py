"""Catalog of the ``warning`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "CARDINALITY_MATCH_DEVIATION",
    "FIT_JOINT_CELLS_TRUNCATED",
    "FORMAT_RULE_VIOLATION",
    "FPE_JOIN_GROUP_ACTIVE",
    "FPE_PARTIAL_PLAINTEXT_DISCLOSURE",
    "GENERATION_CONDITION_PARENT_UNSEEN",
    "GENERATION_FIDELITY_BELOW_THRESHOLD",
    "GEO_GENERALIZE_CASCADE",
    "JOB_LOG_WRITE_FAILED",
    "LIBRARY_DEGRADED",
    "LIBRARY_DELETE_SKIPPED",
    "LIBRARY_WRITE_FAILED",
    "MIMESIS_LOCALE_FALLBACK",
    "NESTED_CELL_JSON_PARSE_ERROR",
    "NESTED_JSONPATH_PATH_OVERLAP",
    "ORPHAN_FK",
    "OUT_OF_CORE_CALLER_MANAGED_RESIDENCY",
    "POOL_DOMINATES_CACHE",
    "SCAN_CRASHED",
    "SCAN_HEADER_SUSPECT",
    "SCAN_KEY_TYPE_MISMATCH",
    "SEED_IGNORED_WITH_MASTER_KEY",
    "SOURCE_VALUE_LEAK",
    "TEXT_MASK_SUB_FLOOR_SPAN_HANDLED",
    "TOP_CODE_GENERALIZED",
    "UNDECLARED_OUTPUT_COLUMNS",
    "VALUE_REUSE_FIXED_POINT",
]

_d = _catalog.warning("QualityWarning")

CARDINALITY_MATCH_DEVIATION = _d("cardinality_match_deviation", "Cardinality match deviation.")
FIT_JOINT_CELLS_TRUNCATED = _d(
    "fit_joint_cells_truncated", "Fit joint cells truncated.", since="1.22.0"
)
FORMAT_RULE_VIOLATION = _d("format_rule_violation", "Format rule violation.")
FPE_JOIN_GROUP_ACTIVE = _d("fpe_join_group_active", "FPE join group active.")
FPE_PARTIAL_PLAINTEXT_DISCLOSURE = _d(
    "fpe_partial_plaintext_disclosure", "FPE partial plaintext disclosure."
)
GENERATION_CONDITION_PARENT_UNSEEN = _d(
    "generation_condition_parent_unseen",
    "Conditional column drew some rows from its overall counts.",
    since="1.22.0",
)
GENERATION_FIDELITY_BELOW_THRESHOLD = _d(
    "generation_fidelity_below_threshold",
    "Generated table scores below the fidelity warn threshold.",
    since="1.22.0",
)
GEO_GENERALIZE_CASCADE = _d("geo_generalize_cascade", "Geo generalize cascade.")
MIMESIS_LOCALE_FALLBACK = _d("mimesis_locale_fallback", "Mimesis locale fallback.")
NESTED_CELL_JSON_PARSE_ERROR = _d("nested_cell_json_parse_error", "Nested cell JSON parse error.")
NESTED_JSONPATH_PATH_OVERLAP = _d("nested_jsonpath_path_overlap", "Nested jsonpath path overlap.")
ORPHAN_FK = _d("orphan_fk", "Orphan FK.")
OUT_OF_CORE_CALLER_MANAGED_RESIDENCY = _d(
    "out_of_core_caller_managed_residency", "Out of core caller managed residency."
)
POOL_DOMINATES_CACHE = _d("pool_dominates_cache", "Pool dominates cache.")
SCAN_CRASHED = _d("scan_crashed", "Scan crashed.")
SCAN_HEADER_SUSPECT = _d(
    "scan_header_suspect", "First row looks like data, not a header.", since="1.10.0"
)
SCAN_KEY_TYPE_MISMATCH = _d(
    "scan_key_type_mismatch", "Relationship key columns differ in type.", since="1.10.0"
)
SEED_IGNORED_WITH_MASTER_KEY = _d(
    "seed_ignored_with_master_key",
    "The job seed is not used for generated tables when a master key is set.",
    since="1.22.0",
)
SOURCE_VALUE_LEAK = _d("source_value_leak", "Source value leak.")
TEXT_MASK_SUB_FLOOR_SPAN_HANDLED = _d(
    "text_mask_sub_floor_span_handled", "Text mask sub floor span handled."
)
TOP_CODE_GENERALIZED = _d("top_code_generalized", "Top code generalized.")
UNDECLARED_OUTPUT_COLUMNS = _d("undeclared_output_columns", "Undeclared output columns.")
VALUE_REUSE_FIXED_POINT = _d("value_reuse_fixed_point", "Value reuse fixed point.")

# Events the CLI builds around the local record library; they are registered here because an
# unregistered code reaches the wire as `unregistered_reason`.
_d = _catalog.warning("LibraryEvent")

JOB_LOG_WRITE_FAILED = _d("job_log_write_failed", "Job log write failed.", since="1.20.0")
LIBRARY_DEGRADED = _d("library_degraded", "Library degraded.", since="1.20.0")
LIBRARY_DELETE_SKIPPED = _d("library_delete_skipped", "Library delete skipped.", since="1.20.0")
LIBRARY_WRITE_FAILED = _d("library_write_failed", "Library write failed.", since="1.20.0")

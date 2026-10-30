"""Catalog of the ``warning`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "CARDINALITY_MATCH_DEVIATION",
    "FORMAT_RULE_VIOLATION",
    "FPE_JOIN_GROUP_ACTIVE",
    "FPE_PARTIAL_PLAINTEXT_DISCLOSURE",
    "GEO_GENERALIZE_CASCADE",
    "MIMESIS_LOCALE_FALLBACK",
    "NESTED_CELL_JSON_PARSE_ERROR",
    "NESTED_JSONPATH_PATH_OVERLAP",
    "ORPHAN_FK",
    "OUT_OF_CORE_CALLER_MANAGED_RESIDENCY",
    "POOL_DOMINATES_CACHE",
    "SCAN_CRASHED",
    "SOURCE_VALUE_LEAK",
    "TEXT_MASK_SUB_FLOOR_SPAN_HANDLED",
    "TOP_CODE_GENERALIZED",
    "UNDECLARED_OUTPUT_COLUMNS",
    "VALUE_REUSE_FIXED_POINT",
]

_d = _catalog.warning("QualityWarning")

CARDINALITY_MATCH_DEVIATION = _d("cardinality_match_deviation", "Cardinality match deviation.")
FORMAT_RULE_VIOLATION = _d("format_rule_violation", "Format rule violation.")
FPE_JOIN_GROUP_ACTIVE = _d("fpe_join_group_active", "FPE join group active.")
FPE_PARTIAL_PLAINTEXT_DISCLOSURE = _d(
    "fpe_partial_plaintext_disclosure", "FPE partial plaintext disclosure."
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
SOURCE_VALUE_LEAK = _d("source_value_leak", "Source value leak.")
TEXT_MASK_SUB_FLOOR_SPAN_HANDLED = _d(
    "text_mask_sub_floor_span_handled", "Text mask sub floor span handled."
)
TOP_CODE_GENERALIZED = _d("top_code_generalized", "Top code generalized.")
UNDECLARED_OUTPUT_COLUMNS = _d("undeclared_output_columns", "Undeclared output columns.")
VALUE_REUSE_FIXED_POINT = _d("value_reuse_fixed_point", "Value reuse fixed point.")

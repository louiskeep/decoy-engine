"""Catalog of the ``plan`` namespace, part 3."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "DEDUPE_UNSUPPORTED_TYPE",
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
STATISTICAL_SPEC_NOT_PINNED = _d("statistical_spec_not_pinned", "Statistical spec not pinned.")
STATISTICAL_STATS_DEGENERATE = _d("statistical_stats_degenerate", "Statistical stats degenerate.")

_d = _catalog.plan("decoy_engine.execution._transforms")

DEDUPE_UNSUPPORTED_TYPE = _d("dedupe_unsupported_type", "Dedupe unsupported type.")
SORT_UNSUPPORTED_TYPE = _d("sort_unsupported_type", "Sort unsupported type.")

_d = _catalog.plan("decoy_engine.plan._checks_top_code")

TOP_CODE_INVALID_FLOOR = _d("top_code_invalid_floor", "Top code invalid floor.")

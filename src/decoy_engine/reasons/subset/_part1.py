"""Catalog of the ``subset`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "SUBSET_BUDGET_EXCEEDED",
    "SUBSET_RELATIONSHIP_COLUMN_MISSING",
    "SUBSET_RELATIONSHIP_COMPOSITE_LENGTH",
    "SUBSET_RELATIONSHIP_DUPLICATE_COLUMN",
    "SUBSET_RELATIONSHIP_KEY_FLOAT_UNSUPPORTED",
    "SUBSET_RELATIONSHIP_TYPE_MISMATCH",
    "SUBSET_REQUIRES_PARQUET",
    "SUBSET_RESERVED_COLUMN",
    "SUBSET_SOURCE_ORPHANS",
    "SUBSET_UNKNOWN_TABLE",
]

_d = _catalog.subset("PreflightFailure")

SUBSET_RELATIONSHIP_COLUMN_MISSING = _d(
    "subset_relationship_column_missing", "Subset relationship column missing."
)
SUBSET_RELATIONSHIP_COMPOSITE_LENGTH = _d(
    "subset_relationship_composite_length", "Subset relationship composite length."
)
SUBSET_RELATIONSHIP_DUPLICATE_COLUMN = _d(
    "subset_relationship_duplicate_column", "Subset relationship duplicate column."
)
SUBSET_RELATIONSHIP_KEY_FLOAT_UNSUPPORTED = _d(
    "subset_relationship_key_float_unsupported", "Subset relationship key float unsupported."
)
SUBSET_RELATIONSHIP_TYPE_MISMATCH = _d(
    "subset_relationship_type_mismatch", "Subset relationship type mismatch."
)
SUBSET_REQUIRES_PARQUET = _d("subset_requires_parquet", "Subset requires Parquet.")
SUBSET_RESERVED_COLUMN = _d("subset_reserved_column", "Subset reserved column.")
SUBSET_SOURCE_ORPHANS = _d("subset_source_orphans", "Subset source orphans.")
SUBSET_UNKNOWN_TABLE = _d("subset_unknown_table", "Subset unknown table.")

_d = _catalog.subset("SubsetBudgetExceededError")

SUBSET_BUDGET_EXCEEDED = _d("subset_budget_exceeded", "Subset budget exceeded.")

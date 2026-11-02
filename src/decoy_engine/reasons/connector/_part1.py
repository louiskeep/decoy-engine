"""Catalog of the ``connector`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "AMBIGUOUS_NAME",
    "CATALOG_READ_FAILED",
    "COLUMN_NOT_FOUND",
    "DEADLINE_EXCEEDED",
    "DUCKDB_UNAVAILABLE",
    "DUPLICATE_RELATION",
    "EXPORT_FAILED",
    "EXTENSION_UNAVAILABLE",
    "EXTENSION_UNTRUSTED_LOCATION",
    "NO_SELECTIONS",
    "RELATION_NOT_FOUND",
    "SOURCE_UNREACHABLE",
    "UNSUPPORTED_COLUMN_TYPE",
    "VALUE_GUARD_FAILED",
    "VALUE_NOT_REPRESENTABLE",
]

_d = _catalog.connector("PostgresSnapshotError")

AMBIGUOUS_NAME = _d("ambiguous_name", "Ambiguous name.")
CATALOG_READ_FAILED = _d("catalog_read_failed", "Catalog read failed.")
COLUMN_NOT_FOUND = _d("column_not_found", "Column not found.")
DEADLINE_EXCEEDED = _d("deadline_exceeded", "Deadline exceeded.")
DUCKDB_UNAVAILABLE = _d("duckdb_unavailable", "Duckdb unavailable.")
DUPLICATE_RELATION = _d("duplicate_relation", "Duplicate relation.")
EXPORT_FAILED = _d("export_failed", "Export failed.")
EXTENSION_UNAVAILABLE = _d("extension_unavailable", "Extension unavailable.")
EXTENSION_UNTRUSTED_LOCATION = _d("extension_untrusted_location", "Extension untrusted location.")
NO_SELECTIONS = _d("no_selections", "No selections.")
RELATION_NOT_FOUND = _d("relation_not_found", "Relation not found.")
SOURCE_UNREACHABLE = _d("source_unreachable", "Source unreachable.")
UNSUPPORTED_COLUMN_TYPE = _d("unsupported_column_type", "Unsupported column type.")
VALUE_GUARD_FAILED = _d("value_guard_failed", "Value guard failed.")
VALUE_NOT_REPRESENTABLE = _d("value_not_representable", "Value not representable.")

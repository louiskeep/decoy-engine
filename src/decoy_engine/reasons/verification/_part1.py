"""Catalog of the ``verification`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "CARDINALITY",
    "COLUMN_SURVIVAL",
    "COMPOSITE_COHERENCE",
    "DETERMINISM_SAMPLE",
    "FK_VALIDITY",
    "FORMAT_RULES",
    "GOF_UNAVAILABLE",
    "KIND_DRIFT",
    "LEAKAGE",
    "NO_COMPARABLE_COLUMNS",
    "NO_COMPARABLE_JOINTS",
    "NULL_AUDIT",
    "NULL_DRIFT",
    "PK_UNIQUENESS",
    "ROW_COUNT",
    "SAMPLED_VALUES",
]

_d = _catalog.verification("PostValidationScan")

CARDINALITY = _d("cardinality", "Cardinality.")
COMPOSITE_COHERENCE = _d("composite_coherence", "Composite coherence.")
DETERMINISM_SAMPLE = _d("determinism_sample", "Determinism sample.")
FK_VALIDITY = _d("fk_validity", "FK validity.")
FORMAT_RULES = _d("format_rules", "Format rules.")
LEAKAGE = _d("leakage", "Leakage.")
NULL_AUDIT = _d("null_audit", "Null audit.")
PK_UNIQUENESS = _d("pk_uniqueness", "PK uniqueness.")
SAMPLED_VALUES = _d("sampled_values", "Sampled values.")

_d = _catalog.verification("QualityDiagnostic")

COLUMN_SURVIVAL = _d("column_survival", "Column survival.")
KIND_DRIFT = _d("kind_drift", "Kind drift.")
NULL_DRIFT = _d("null_drift", "Null drift.")
ROW_COUNT = _d("row_count", "Row count.")

_d = _catalog.verification("QualityReport")

GOF_UNAVAILABLE = _d("gof_unavailable", "GOF unavailable.")
NO_COMPARABLE_COLUMNS = _d("no_comparable_columns", "No comparable columns.")
NO_COMPARABLE_JOINTS = _d("no_comparable_joints", "No comparable joints.")

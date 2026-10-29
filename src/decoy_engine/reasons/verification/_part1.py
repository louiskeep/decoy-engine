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

CARDINALITY = _d("cardinality", "The cardinality check reported a finding.")
COMPOSITE_COHERENCE = _d("composite_coherence", "The composite coherence check reported a finding.")
DETERMINISM_SAMPLE = _d("determinism_sample", "The determinism sample check reported a finding.")
FK_VALIDITY = _d("fk_validity", "The fk validity check reported a finding.")
FORMAT_RULES = _d("format_rules", "The format rules check reported a finding.")
LEAKAGE = _d("leakage", "The leakage check reported a finding.")
NULL_AUDIT = _d("null_audit", "The null audit check reported a finding.")
PK_UNIQUENESS = _d("pk_uniqueness", "The pk uniqueness check reported a finding.")
SAMPLED_VALUES = _d("sampled_values", "The sampled values check reported a finding.")

_d = _catalog.verification("QualityDiagnostic")

COLUMN_SURVIVAL = _d("column_survival", "The column survival check reported a finding.")
KIND_DRIFT = _d("kind_drift", "The kind drift check reported a finding.")
NULL_DRIFT = _d("null_drift", "The null drift check reported a finding.")
ROW_COUNT = _d("row_count", "The row count check reported a finding.")

_d = _catalog.verification("QualityReport")

GOF_UNAVAILABLE = _d("gof_unavailable", "The gof unavailable check reported a finding.")
NO_COMPARABLE_COLUMNS = _d(
    "no_comparable_columns", "The no comparable columns check reported a finding."
)
NO_COMPARABLE_JOINTS = _d(
    "no_comparable_joints", "The no comparable joints check reported a finding."
)

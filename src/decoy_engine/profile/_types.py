"""Frozen dataclasses describing a source-data profile.

All types here are immutable input/output for the planner: snapshots taken
once, compared by value, serialized to JSON for evidence and manifests.
Mutation is not supported; construct a new instance to "modify" a profile.

The split between data-shape fields (included in profile_hash) and sidecar
metadata (excluded) is the resolution of B1 in the Dennis spec review. See
the profile_hash docstring for the rationale and the
docs/v2/reviews/dennis-engine-v2-s1-spec-review-2026-05-26.md artifact.

PIIClass enumerates the built-in STORM detector ids (resolution of M1).
Custom-detector matches (CustomDetectorSpec ids in
decoy_engine.storm.types) are not represented in this enum; columns
matched only by a custom detector carry pii_class=None at the Profile
layer. A V2+ extension can add a sibling custom_pii_class field if needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class PIIClass(str, Enum):
    """Built-in STORM detector ids.

    Mirrors the detector_id values produced by
    decoy_engine.storm.detectors. Keep in sync as STORM grows its
    built-in detector set; custom detectors stay outside this enum.
    """

    # Core PII
    EMAIL = "email"
    SSN = "ssn"
    US_PHONE = "us_phone"
    US_ZIP = "us_zip"
    PERSON_NAME = "person_name"
    ISO_DATE = "iso_date"
    US_DATE = "us_date"
    EU_DATE = "eu_date"
    # PCI / GDPR
    PAN = "pan"
    CVV = "cvv"
    IBAN = "iban"
    IPV4 = "ipv4"
    # HIPAA Safe Harbor + clinical
    ICD10 = "icd10"
    NPI = "npi"
    MRN = "mrn"
    URL = "url"
    FAX_NUMBER = "fax_number"
    HEALTH_PLAN_ID = "health_plan_id"
    LICENSE_NUM = "license_num"
    VEHICLE_ID = "vehicle_id"
    DEVICE_ID = "device_id"
    BIOMETRIC_ID = "biometric_id"


@dataclass(frozen=True)
class ColumnProfile:
    """Column-level statistics from a source-data walk.

    is_candidate_key_sampled enforces the H6 invariant: it is only True
    when the column passed a definitive (non-sampled) full-scan
    uniqueness check. Sample-only uniqueness reads are not load-bearing
    and the planner must not treat them as candidate keys. The
    __post_init__ rejects the contradictory combination.
    """

    name: str
    dtype: str
    row_count: int
    null_count: int
    distinct_count: int | None
    sampled: bool
    is_candidate_key_sampled: bool
    declared_pk: bool
    is_fk: bool
    fk_target: tuple[str, str] | None
    pii_class: PIIClass | None

    def __post_init__(self) -> None:
        if self.sampled and self.is_candidate_key_sampled:
            raise ValueError(
                f"ColumnProfile {self.name!r}: is_candidate_key_sampled must be "
                "False when sampled=True. A definitive candidate-key flag "
                "requires a full-scan profile (sample_rows=None). See H6 in "
                "the engine-v2 S1 spec review."
            )


@dataclass(frozen=True)
class Relationship:
    """A foreign-key relationship between a parent column tuple and a child column tuple.

    Composite keys are represented by length>1 column tuples. Single-column
    relationships are length-1 tuples. parent_columns and child_columns must
    have the same length; the plan-compile composite_columns_length_match
    check enforces this when consuming a Profile. See B2 in the engine-v2
    S1 spec review.
    """

    parent_table: str
    parent_columns: tuple[str, ...]
    child_table: str
    child_columns: tuple[str, ...]
    namespace: str | None


@dataclass(frozen=True)
class TableProfile:
    name: str
    row_count: int
    columns: tuple[ColumnProfile, ...]


@dataclass(frozen=True)
class Profile:
    """Source-data profile: data-shape fields plus sidecar metadata.

    profile_hash is computed over the data-shape group only. Sidecar
    metadata (profiled_at, decoy_engine_version, profile_seed) is
    excluded so two profiles over identical source data produce equal
    hashes regardless of when or by which engine build they were taken.
    See B1 in the engine-v2 S1 spec review and the profile_hash
    docstring for the canonical-bytes definition.
    """

    # Data-shape fields (hashed)
    schema_version: int
    tables: tuple[TableProfile, ...]
    relationships: tuple[Relationship, ...]

    # Sidecar metadata (NOT hashed)
    profiled_at: datetime
    decoy_engine_version: str
    profile_seed: int | None = field(default=None)

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

    Mirrors the detector_id values produced by every entry in
    decoy_engine.storm.detectors.REGISTERED_DETECTORS. The enum and the
    registry are kept in sync by tests/unit/profile/test_pii_storm_sync.py
    which fails if a new STORM built-in lands without a matching enum
    member (resolution of slice-3 B1: first_name, last_name, address were
    added to REGISTERED_DETECTORS in the V1 detection sprint but never
    propagated to this enum, causing those columns to drop their tags
    silently during run_pii_detection=True walks).

    Custom detectors (CustomDetectorSpec ids conventionally prefixed
    "custom__") stay outside this enum by design.
    """

    # Core PII
    EMAIL = "email"
    SSN = "ssn"
    US_PHONE = "us_phone"
    US_ZIP = "us_zip"
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    PERSON_NAME = "person_name"
    ADDRESS = "address"
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
        # is_fk <-> fk_target consistency (L1 from slice-1 review).
        if self.is_fk != (self.fk_target is not None):
            raise ValueError(
                f"ColumnProfile {self.name!r}: is_fk={self.is_fk} but "
                f"fk_target={self.fk_target!r}; these must agree "
                "(both set or both unset)."
            )
        # Cardinality sanity (L2 from slice-1 review). A profile_source
        # implementation that violates either of these has a bug; failing
        # loud at construction time keeps that bug out of downstream consumers.
        if self.null_count > self.row_count:
            raise ValueError(
                f"ColumnProfile {self.name!r}: null_count={self.null_count} "
                f"exceeds row_count={self.row_count}."
            )
        if self.distinct_count is not None and self.distinct_count > self.row_count:
            raise ValueError(
                f"ColumnProfile {self.name!r}: distinct_count={self.distinct_count} "
                f"exceeds row_count={self.row_count}."
            )


@dataclass(frozen=True)
class Relationship:
    """A foreign-key relationship between a parent column tuple and a child column tuple.

    Composite keys are represented by length>1 column tuples. Single-column
    relationships are length-1 tuples. parent_columns and child_columns must
    have the same length and both must be non-empty; the dataclass enforces
    this at construction (M1 from the slice-1 review) so a malformed
    Relationship cannot survive a serialization round-trip even when no
    planner is in the loop. The plan-compile composite_columns_length_match
    check (#5) is the equivalent guard at the Plan layer. See B2 in the
    engine-v2 S1 spec review.
    """

    parent_table: str
    parent_columns: tuple[str, ...]
    child_table: str
    child_columns: tuple[str, ...]
    namespace: str | None

    def __post_init__(self) -> None:
        if len(self.parent_columns) == 0 or len(self.child_columns) == 0:
            raise ValueError(
                f"Relationship {self.parent_table}->{self.child_table}: "
                "parent_columns and child_columns must both be non-empty."
            )
        if len(self.parent_columns) != len(self.child_columns):
            raise ValueError(
                f"Relationship {self.parent_table}.{self.parent_columns} -> "
                f"{self.child_table}.{self.child_columns}: parent_columns "
                f"length {len(self.parent_columns)} != child_columns length "
                f"{len(self.child_columns)}."
            )


@dataclass(frozen=True)
class TableProfile:
    """A table-level profile: a name, a row count, and an ordered column tuple.

    Column names are unique within the table (M2 from the slice-1 review);
    duplicate names cause silent column loss in any dict-keyed consumer.
    The check fails loud at construction time.
    """

    name: str
    row_count: int
    columns: tuple[ColumnProfile, ...]

    def __post_init__(self) -> None:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            seen: set[str] = set()
            dupes: set[str] = set()
            for n in names:
                if n in seen:
                    dupes.add(n)
                seen.add(n)
            raise ValueError(
                f"TableProfile {self.name!r}: duplicate column names "
                f"{sorted(dupes)!r}; column names must be unique within a table."
            )


@dataclass(frozen=True)
class Profile:
    """Source-data profile: data-shape fields plus sidecar metadata.

    profile_hash is computed over the data-shape group only. Sidecar
    metadata (profiled_at, decoy_engine_version, profile_seed) is
    excluded so two profiles over identical source data produce equal
    hashes regardless of when or by which engine build they were taken.
    See B1 in the engine-v2 S1 spec review and the profile_hash
    docstring for the canonical-bytes definition.

    Table names are unique within a Profile (M3 from the slice-1 review).
    Cross-table FK validation (every Relationship.parent_table /
    .child_table references a real TableProfile and the named columns
    exist) is deferred to the planner: it is cross-cutting validation
    rather than a Profile-layer invariant, and the planner already owns
    the FK-DAG construction.
    """

    # Data-shape fields (hashed)
    schema_version: int
    tables: tuple[TableProfile, ...]
    relationships: tuple[Relationship, ...]

    # Sidecar metadata (NOT hashed)
    profiled_at: datetime
    decoy_engine_version: str
    profile_seed: int | None = field(default=None)

    def __post_init__(self) -> None:
        names = [t.name for t in self.tables]
        if len(set(names)) != len(names):
            seen: set[str] = set()
            dupes: set[str] = set()
            for n in names:
                if n in seen:
                    dupes.add(n)
                seen.add(n)
            raise ValueError(
                f"Profile: duplicate table names {sorted(dupes)!r}; "
                "table names must be unique within a Profile."
            )

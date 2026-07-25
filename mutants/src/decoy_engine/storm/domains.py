"""Semantic domains over STORM detector IDs (gap-closure item 1).

Detector IDs (``"ssn"``, ``"email"``, ...) are precise but ad-hoc; product
surfaces want to group and filter by a human *domain* (Identity, Financial,
Health, Contact, Location). This module promotes the bare strings into a
named :class:`Domain` enum plus a total ``detector_id -> Domain`` table.

The mapping is **derived, never persisted**: detection still records bare
detector IDs (so ``StormScan.column_overrides`` stays stable); ``domain`` is
attached at construction for display only.

Taxonomy source pattern: the HEALTH and date/geo identifiers follow the
HIPAA Safe Harbor 18-identifier list (45 CFR 164.514(b)(2)); the
Identity / Financial / Contact / Location split follows a generic
direct-vs-quasi PII taxonomy (NIST SP 800-122 PII categories,
ISO/IEC 29100). Ambiguous picks are documented inline so they read as a
decision, not an accident.
"""

from __future__ import annotations

from enum import Enum


class Domain(str, Enum):
    """Human-facing semantic category for a detector. str-Enum so
    ``dataclasses.asdict`` / JSON serialize the bare value string."""

    IDENTITY = "IDENTITY"
    FINANCIAL = "FINANCIAL"
    HEALTH = "HEALTH"
    CONTACT = "CONTACT"
    LOCATION = "LOCATION"
    OTHER = "OTHER"


# Total over every detector registered in ``storm.detectors``. Kept as a pure
# data table so the engine stays narrow (the platform/web derive from it).
# Decisions worth noting:
#   - dates (iso/us/eu) -> IDENTITY: dates of birth/admission are HIPAA
#     identifiers; treated as identity-class quasi-identifiers here.
#   - ipv4, vehicle_id -> IDENTITY: device/asset identifiers tied to a person.
#   - url -> CONTACT: a personal/profile URL is a contact handle.
DOMAIN_BY_DETECTOR: dict[str, Domain] = {
    # Identity (direct + quasi identifiers of a person)
    "first_name": Domain.IDENTITY,
    "last_name": Domain.IDENTITY,
    "person_name": Domain.IDENTITY,
    "ssn": Domain.IDENTITY,
    "ipv4": Domain.IDENTITY,
    "vehicle_id": Domain.IDENTITY,
    "iso_date": Domain.IDENTITY,
    "us_date": Domain.IDENTITY,
    "eu_date": Domain.IDENTITY,
    # Contact (ways to reach a person)
    "email": Domain.CONTACT,
    "us_phone": Domain.CONTACT,
    "fax_number": Domain.CONTACT,
    "url": Domain.CONTACT,
    # Location (where a person is)
    "address": Domain.LOCATION,
    "us_zip": Domain.LOCATION,
    # Financial
    "pan": Domain.FINANCIAL,
    "cvv": Domain.FINANCIAL,
    "iban": Domain.FINANCIAL,
    # Health (HIPAA Safe Harbor identifiers + clinical codes)
    "mrn": Domain.HEALTH,
    "npi": Domain.HEALTH,
    "icd10": Domain.HEALTH,
    "health_plan_id": Domain.HEALTH,
    "license_num": Domain.HEALTH,
    "device_id": Domain.HEALTH,
    "biometric_id": Domain.HEALTH,
}


def domain_for(detector_id: str) -> Domain:
    """Return the semantic domain for a detector id.

    Unknown or custom ids (``custom__*``) map to :attr:`Domain.OTHER` rather
    than raising, so future or user-defined detectors never crash a profile.
    """
    return DOMAIN_BY_DETECTOR.get(detector_id, Domain.OTHER)


def registered_detector_ids() -> frozenset[str]:
    """The detector ids the built-in registry exposes.

    Imported lazily to avoid a module-load cycle with ``storm.detectors``
    (which imports this module to stamp ``domain`` at match construction).
    """
    from decoy_engine.storm.detectors import REGISTERED_DETECTORS

    return frozenset(fn.__name__.removeprefix("detect_") for fn in REGISTERED_DETECTORS)

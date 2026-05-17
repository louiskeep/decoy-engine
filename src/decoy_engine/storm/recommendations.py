"""Smart-default mask strategy per detector.

Single source of truth that FORECAST and the platform's column-override
endpoint both consume. When STORM (or a manual override) attaches a
detector_id to a column, this table answers "what's the right default
mask strategy + params?" so the user gets a sensible starting point
without re-deriving it in three places.

The user can always edit after the default is applied — the values here
are starting points, not locks. Detection sprint (V1) chose conservative
defaults that prioritize safety (format-preserving where it's well-
defined, redact otherwise) over ergonomics.

Cross-references:
  - decoy-platform/docs/product/format-preservation.md publishes the
    user-visible coverage table sourced from this module.
  - decoy-platform/docs/backlog/v2/plans/decoy-platform/2026-05-17-detection-and-fpe-gaps.md
    tracks the redact-default detectors that V2 will upgrade to
    semantic-aware FPE (icd10, url, license_num, ...).
"""

from __future__ import annotations

from typing import Any, Optional


# (strategy_name, params_dict) — strategy_name is the canonical mask kind
# (faker.first_name / fpe / date_shift / redact / ...). The platform
# routes these through the mask registry without further translation.
StrategyDefault = tuple[str, dict[str, Any]]


DEFAULT_STRATEGY_BY_DETECTOR: dict[str, StrategyDefault] = {
    # ── personal identifiers (names + contact) ──────────────────────────
    "first_name":      ("faker.first_name",   {"locale": "en_US"}),
    "last_name":       ("faker.last_name",    {"locale": "en_US"}),
    "person_name":     ("faker.name",         {"locale": "en_US"}),
    "email":           ("faker.email",        {"unique_preserving": True}),
    "us_phone":        ("faker.phone_number", {"locale": "en_US"}),
    "fax_number":      ("faker.phone_number", {"locale": "en_US"}),
    "address":         ("faker.address",      {"locale": "en_US"}),

    # ── government / national identifiers ───────────────────────────────
    "ssn":             ("fpe", {"alphabet": "digits", "length": 9}),

    # ── dates ───────────────────────────────────────────────────────────
    "iso_date":        ("date_shift", {"window_days": 15, "deterministic": True}),
    "us_date":         ("date_shift", {"window_days": 15, "deterministic": True}),
    "eu_date":         ("date_shift", {"window_days": 15, "deterministic": True}),

    # ── geolocation ─────────────────────────────────────────────────────
    "us_zip":          ("fpe", {"alphabet": "digits", "length": 5}),
    "ipv4":            ("faker.ipv4", {}),

    # ── healthcare identifiers ──────────────────────────────────────────
    "mrn":             ("fpe", {"alphabet": "alphanumeric", "preserve_length": True}),
    "npi":             ("fpe", {"alphabet": "digits", "length": 10}),

    # ── financial identifiers ───────────────────────────────────────────
    "pan":             ("fpe", {"alphabet": "digits", "length": 16, "luhn_preserving": True}),
    "cvv":             ("redact", {"value": "XXX"}),
    "iban":            ("fpe", {"preserve_country_prefix": True, "recompute_check_digits": True}),

    # ── vehicle ─────────────────────────────────────────────────────────
    "vehicle_id":      ("fpe", {"alphabet": "alphanumeric", "length": 17}),

    # ── redact-default group ────────────────────────────────────────────
    # Semantic / structural format preservation is V2 (see gap doc).
    # Until then, redact is the safe default — preserving "shape" without
    # semantics would be misleading (e.g. a fake ICD-10 that happens to
    # mean a different disease).
    "icd10":           ("redact", {"value": "REDACTED"}),
    "url":             ("redact", {"value": "REDACTED"}),
    "license_num":     ("redact", {"value": "REDACTED"}),
    "health_plan_id":  ("redact", {"value": "REDACTED"}),
    "device_id":       ("redact", {"value": "REDACTED"}),
    "biometric_id":    ("redact", {"value": "REDACTED"}),
}


# Valid strategy names accepted across the mask layer. Used by the
# get_default_strategy validator + the test suite to detect typos in the
# lookup above. Adding a new strategy here without wiring it in the mask
# registry is harmless — it just widens the validator. Removing one
# without updating the lookup will break the regression test.
VALID_STRATEGIES: frozenset[str] = frozenset({
    "faker.first_name",
    "faker.last_name",
    "faker.name",
    "faker.email",
    "faker.phone_number",
    "faker.address",
    "faker.ipv4",
    "fpe",
    "date_shift",
    "redact",
})


def get_default_strategy(detector_id: str) -> Optional[StrategyDefault]:
    """Return the default (strategy_name, params) for a detector_id.

    Returns None when the detector_id isn't in the table — callers should
    treat that as "no smart default; leave the column to manual config".
    Custom detector ids (e.g. "custom__uk_nhs_number") will always miss
    this table by design.
    """
    return DEFAULT_STRATEGY_BY_DETECTOR.get(detector_id)


def known_detector_ids() -> frozenset[str]:
    """The set of built-in detector_ids that have a smart-default strategy.

    Used by the override UI to populate the "Set field type" dropdown
    without re-listing detectors in the platform layer.
    """
    return frozenset(DEFAULT_STRATEGY_BY_DETECTOR.keys())

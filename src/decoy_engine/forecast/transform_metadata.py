"""Declarative metadata about each masking transform.

FORECAST consults this when a column has detector hits but there's no
Disguise recommending a specific Mask for it. For each detector id we
keep an ordered list of (mask, params, why) tuples — the first one wins
and is surfaced as the recommendation; the rest are alternatives the UI
can show.

Design choice: keep the metadata as a flat python dict, not a class
hierarchy. Every transform's row in the table is one tuple; adding or
reordering recommendations is a single edit. No imports of the actual
strategy classes — FORECAST is a pure function over names.
"""

from __future__ import annotations

from typing import Any


# (mask_id, params, one-line why)
TransformChoice = tuple[str, dict[str, Any], str]


# Detector → ranked transforms. The first entry is the default recommendation.
DETECTOR_TO_TRANSFORMS: dict[str, list[TransformChoice]] = {
    "email": [
        ("faker", {"faker_type": "email"}, "Replace with a deterministic fake email."),
        ("hash",  {"algorithm": "sha256"}, "Hash the email; preserves uniqueness for joins, not reversible."),
        ("redact", {"keep_chars": 0},      "Blank out entirely."),
    ],
    "ssn": [
        ("hash",  {"algorithm": "sha256"}, "Hash the SSN; preserves uniqueness for joins, not reversible."),
        ("faker", {"faker_type": "ssn"},   "Replace with a fake SSN-formatted value."),
        ("redact", {"keep_chars": 0},      "Blank the field entirely (most aggressive)."),
    ],
    "us_phone": [
        ("faker", {"faker_type": "phone_number"}, "Replace with a fake phone number."),
        ("hash",  {"algorithm": "sha256"},        "Hash; preserves uniqueness, not reversible."),
    ],
    "us_zip": [
        ("redact", {"keep_chars": 3},             "Keep first 3 digits (HIPAA Safe Harbor for ZCTAs >20K population)."),
        # ``zipcode`` is what the engine's provider whitelist exposes
        # for US-style ZIPs (helpers.py). ``postcode`` is a real Faker
        # method but isn't registered, so the engine would warn and
        # fall back to ``word`` once per row.
        ("faker",  {"faker_type": "zipcode"},     "Replace with a fake ZIP."),
    ],
    "person_name": [
        ("faker", {"faker_type": "name"}, "Replace with a realistic fake name."),
        ("hash",  {"algorithm": "sha256"}, "Hash the name; preserves uniqueness, not reversible."),
    ],
    "iso_date": [
        ("date_shift", {"jitter_days": 30},       "Shift date by ±30 days; preserves distribution, breaks correlation."),
        ("faker",      {"faker_type": "date"},    "Replace with a random plausible date."),
    ],
    "us_date": [
        ("date_shift", {"jitter_days": 30}, "Shift date by ±30 days."),
        ("faker",      {"faker_type": "date"}, "Replace with a random plausible date."),
    ],
    "eu_date": [
        ("date_shift", {"jitter_days": 30}, "Shift date by ±30 days."),
        ("faker",      {"faker_type": "date"}, "Replace with a random plausible date."),
    ],
}


def best_transform_for(detector_id: str) -> TransformChoice | None:
    """Return the top-ranked transform for a detector, or None if unknown."""
    choices = DETECTOR_TO_TRANSFORMS.get(detector_id)
    return choices[0] if choices else None

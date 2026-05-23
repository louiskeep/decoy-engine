"""Declarative metadata about each masking transform.

FORECAST consults this when a column has detector hits but there's no
Disguise recommending a specific Mask for it. For each detector id we
keep a small chooser function (Plan B-2) that reads the column's
FieldStats and returns a (mask, params, why) tuple — params come from
the data instead of being hardcoded constants.

Design choice: keep the metadata as a flat python dict of choosers,
not a class hierarchy. Each detector_id maps to one function; the
fallback ranked list is folded into the chooser as ``alternatives()``
so the UI can still show "what else could you pick" without a second
dict to maintain. No imports of the actual strategy classes — FORECAST
is a pure function over names.

Detection sprint (V1): when a detector_id has no shape-aware chooser
defined here, ``best_transform_for`` falls back to
``DEFAULT_STRATEGY_BY_DETECTOR`` from ``decoy_engine.storm.recommendations``
so newer detectors (first_name, last_name, address, pan, iban, npi,
mrn, etc.) get sensible defaults without forcing every contributor to
add a chooser. Shape-aware choosers stay only where column shape
actually changes the right answer (date jitter scales with span,
phone locale flips with format_pattern, etc.).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from decoy_engine.storm.recommendations import get_default_strategy
from decoy_engine.storm.types import FieldStats

# (mask_id, params, one-line why)
TransformChoice = tuple[str, dict[str, Any], str]

# Plan B-2 — choosers consume FieldStats so params can react to the
# actual column shape. The pre-B-2 module exposed a flat dict of
# tuples; the dict signature is kept for callers that don't pass a
# FieldStats by treating absence as "use the default branch".
Chooser = Callable[[FieldStats | None], TransformChoice]


# ── helpers for chooser branches ─────────────────────────────────────

def _is_high_cardinality(f: FieldStats | None) -> bool:
    """True when the column looks like an identifier (PK / FK / direct ID).

    Reads ``is_likely_unique`` and ``value_set_size_class`` from FieldStats.

    NOTE (Sprint 5 audit 2026-05-22): this helper is currently not called by
    any chooser. The SSN and email choosers were simplified to unconditional
    FPE / faker in V1 (cardinality no longer determines the right answer for
    those detectors). The helper is preserved for future choosers that may
    want to branch on join-key vs display-column behavior. If no chooser
    needs it by V2 planning, delete it.

    Falls back to ``False`` when no FieldStats is passed so chooser defaults
    stay stable for callers (e.g. UI previews) that only know the detector_id.
    """
    if f is None:
        return False
    if f.is_likely_unique:
        return True
    return f.value_set_size_class in ("unique", "high")


def _hash_truncate_for_alphabet(f: FieldStats | None, fallback: int = 12) -> int:
    """Pick a hash.truncate length matched to the column's alphabet.

    Reads ``alphabet`` and ``max_length`` from FieldStats.

    NOTE (Sprint 5 audit 2026-05-22): this helper is currently not called by
    any chooser. It was written for a hash-based SSN/ID chooser that was
    superseded by FPE (which preserves length natively without a truncate
    param). Preserved for future hash-based choosers that may need length
    tuning. If no chooser needs it by V2 planning, delete it.

    Reasoning for the math: hashed output is hex (0-9a-f) regardless of the
    source alphabet, but choosing a truncate that mirrors source character
    classes keeps masked output visually similar -- 9-char SSN-shaped
    numerics truncate to 9 hex chars, 12-char alphanumerics truncate
    to 12, mixed-shape strings keep the conservative 12 default.
    """
    if f is None:
        return fallback
    alphabet = f.alphabet
    max_length = f.max_length or 0
    if alphabet == "digits" and 1 <= max_length <= 64:
        return max_length
    if alphabet == "alphanum" and 1 <= max_length <= 64:
        return min(max_length, 16)
    return fallback


def _faker_locale_for_format(f: FieldStats | None) -> dict[str, Any]:
    """Add a faker locale param when the column's format_pattern is
    locale-specific (e.g. US phone with parens, UK postcode, etc.).
    Returns an empty dict when the format is locale-neutral so the
    chooser can spread the result into its params.
    """
    if f is None or f.format_pattern is None:
        return {}
    fmt = f.format_pattern
    # US phone variants — parentheses-bracketed area code is US-style.
    if "(" in fmt and ")" in fmt:
        return {"locale": "en_US"}
    return {}


# ── per-detector choosers ────────────────────────────────────────────

def _email_chooser(f: FieldStats | None) -> TransformChoice:
    # Detection sprint (V1): faker.email is deterministic when seeded by
    # row, so high-cardinality (unique) email columns no longer need to
    # fall back to hash to preserve joins. Hashing destroys the @ shape
    # and downstream email-format validators reject the output; faker
    # preserves the local@domain.tld pattern and is enough for V1.
    _ = f  # kept for signature symmetry with shape-aware choosers
    return (
        "faker",
        {"faker_type": "email"},
        "Replace with a realistic fake email; deterministic so joins survive.",
    )


def _ssn_chooser(f: FieldStats | None) -> TransformChoice:
    # Detection sprint (V1): FPE preserves the 9-digit shape AND is
    # deterministic by instance key, so high-cardinality SSN columns
    # join cleanly without sacrificing format. Hash was the pre-FPE
    # answer to "joins survive"; FPE solves both problems at once.
    _ = f
    return (
        "fpe",
        {"charset": "digits"},
        "Replace with a format-preserving SSN; deterministic so joins survive, shape stays 9 digits.",
    )


def _phone_chooser(f: FieldStats | None) -> TransformChoice:
    locale = _faker_locale_for_format(f)
    return (
        "faker",
        {"faker_type": "phone_number", **locale},
        "Replace with a fake phone number." + (" Locale picked from source format." if locale else ""),
    )


def _zip_chooser(f: FieldStats | None) -> TransformChoice:
    # HIPAA Safe Harbor — keep first 3 digits when the ZIP is 5 digits
    # in a population-friendly format. When the column already has a
    # ZIP+4 (max_length >= 9) the keep_chars=3 still produces a usable
    # 3-digit prefix, but FORECAST surfaces it explicitly so users can
    # see what's preserved.
    return (
        "redact",
        {"keep_chars": 3},
        "Keep first 3 digits (HIPAA Safe Harbor for ZCTAs >20K population).",
    )


def _person_name_chooser(f: FieldStats | None) -> TransformChoice:
    # Detection sprint (V1): faker.name with row-based seeding is
    # deterministic AND gives effectively unique output at any
    # practical row count. Hash on a name column drops the spaces +
    # capitalization signal users expect to see, which hurts QA.
    _ = f
    return (
        "faker",
        {"faker_type": "name"},
        "Replace with a realistic fake name; deterministic so joins survive.",
    )


def _first_name_chooser(f: FieldStats | None) -> TransformChoice:
    _ = f
    return (
        "faker",
        {"faker_type": "first_name"},
        "Replace with a realistic fake first name.",
    )


def _last_name_chooser(f: FieldStats | None) -> TransformChoice:
    _ = f
    return (
        "faker",
        {"faker_type": "last_name"},
        "Replace with a realistic fake last name.",
    )


def _date_chooser(f: FieldStats | None) -> TransformChoice:
    # B-2 scales jitter to the date span when available. Tight ranges
    # (e.g. a 90-day enrollment window) get ±14d jitter; wide ranges
    # get the default ±30d. Falls back to the constant when STORM
    # didn't pin date_min / date_max (mixed-format columns).
    jitter = 30
    if f is not None and f.date_min and f.date_max:
        try:
            from datetime import date

            lo = date.fromisoformat(f.date_min[:10])
            hi = date.fromisoformat(f.date_max[:10])
            span_days = (hi - lo).days
            if 0 < span_days < 365:
                jitter = max(7, span_days // 4)
        except Exception:
            pass
    return (
        "date_shift",
        {"jitter_days": jitter},
        f"Shift date by ±{jitter} days; preserves distribution, breaks correlation.",
    )


# Detector → chooser. The chooser reads the FieldStats and returns the
# default (mask, params, why) for that detector. ``None`` is a valid
# input for callers that only know the detector id.
DETECTOR_TO_CHOOSER: dict[str, Chooser] = {
    "email": _email_chooser,
    "ssn": _ssn_chooser,
    "us_phone": _phone_chooser,
    "us_zip": _zip_chooser,
    "person_name": _person_name_chooser,
    "first_name": _first_name_chooser,
    "last_name": _last_name_chooser,
    "iso_date": _date_chooser,
    "us_date": _date_chooser,
    "eu_date": _date_chooser,
}


# Detection sprint (V1) — short "why" strings keyed by detector_id, used
# when the recommendation comes from the smart-default fallback instead
# of a shape-aware chooser. Keeps the per-field plan's Why column
# helpful without duplicating the table from the override surface.
_FALLBACK_WHY: dict[str, str] = {
    "fax_number":     "Fax number — replace with a realistic fake.",
    "address":        "Street address — replace with a realistic fake address.",
    "ipv4":           "IP address — replace with a realistic fake IPv4.",
    "mrn":            "Medical record number — format-preserving so joins survive.",
    "npi":            "National Provider Identifier — 10-digit format-preserving.",
    "pan":            "Credit card PAN — format-preserving with Luhn-valid output.",
    "cvv":            "CVV — redacted (PCI DSS §3.2 forbids storage post-auth).",
    "iban":           "IBAN — format-preserving, country prefix kept.",
    "vehicle_id":     "VIN / vehicle identifier — 17-char format-preserving.",
    "icd10":          "ICD-10 code — redacted in V1 (semantic FPE is V2).",
    "url":            "URL — redacted in V1 (structural FPE is V2).",
    "license_num":    "License / certificate — redacted (format varies too widely).",
    "health_plan_id": "Health-plan beneficiary ID — redacted (format varies).",
    "device_id":      "Device identifier — redacted (format varies).",
    "biometric_id":   "Biometric identifier — redacted (always sensitive).",
}


def best_transform_for(
    detector_id: str,
    field: FieldStats | None = None,
) -> TransformChoice | None:
    """Return the top-ranked transform for a detector + field.

    Resolution order:
      1. Shape-aware chooser in ``DETECTOR_TO_CHOOSER``, when present.
      2. ``DEFAULT_STRATEGY_BY_DETECTOR`` from the V1 recommendations
         table, for detectors that have no shape-aware chooser yet.
      3. ``None`` when the detector_id is unknown to both layers.

    ``field`` is optional — callers without a FieldStats get the
    chooser's no-field default.
    """
    chooser = DETECTOR_TO_CHOOSER.get(detector_id)
    if chooser is not None:
        return chooser(field)
    default = get_default_strategy(detector_id)
    if default is None:
        return None
    strategy, params = default
    why = _FALLBACK_WHY.get(detector_id, f"V1 default mask for {detector_id}.")
    return strategy, dict(params), why

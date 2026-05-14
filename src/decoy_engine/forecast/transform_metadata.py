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
"""

from __future__ import annotations

from typing import Any, Callable

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

    Used by detectors that flip mask choice based on whether the column
    is a join key vs a derived display column. Falls back to ``False``
    when no FieldStats is passed so chooser defaults stay stable for
    callers (e.g. UI previews) that only know the detector_id.
    """
    if f is None:
        return False
    if f.is_likely_unique:
        return True
    return f.value_set_size_class in ("unique", "high")


def _hash_truncate_for_alphabet(f: FieldStats | None, fallback: int = 12) -> int:
    """Pick a hash.truncate length matched to the column's alphabet.

    Reasoning: hashed output is hex (0-9a-f) regardless of the source
    alphabet, but choosing a truncate that mirrors source character
    classes keeps masked output visually similar — 9-char SSN-shaped
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
    if _is_high_cardinality(f):
        return (
            "hash",
            {"algorithm": "sha256", "truncate": _hash_truncate_for_alphabet(f, 16)},
            "Email looks like a unique identifier; hash so joins survive.",
        )
    return (
        "faker",
        {"faker_type": "email"},
        "Replace with a deterministic fake email.",
    )


def _ssn_chooser(f: FieldStats | None) -> TransformChoice:
    if _is_high_cardinality(f):
        return (
            "hash",
            {"algorithm": "sha256", "truncate": _hash_truncate_for_alphabet(f, 12)},
            "SSN-shaped identifier; hash preserves uniqueness for joins, not reversible.",
        )
    return (
        "faker",
        {"faker_type": "ssn"},
        "Replace with a fake SSN-formatted value.",
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
    if _is_high_cardinality(f):
        return (
            "hash",
            {"algorithm": "sha256", "truncate": _hash_truncate_for_alphabet(f, 12)},
            "Name column behaves like a join key; hash to preserve uniqueness.",
        )
    return (
        "faker",
        {"faker_type": "name"},
        "Replace with a realistic fake name.",
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
    "iso_date": _date_chooser,
    "us_date": _date_chooser,
    "eu_date": _date_chooser,
}


def best_transform_for(
    detector_id: str,
    field: FieldStats | None = None,
) -> TransformChoice | None:
    """Return the top-ranked transform for a detector + field.

    ``field`` is optional — callers without a FieldStats get the
    chooser's no-field default. Returns ``None`` for unknown detector
    ids so the recommender skips quietly.
    """
    chooser = DETECTOR_TO_CHOOSER.get(detector_id)
    return chooser(field) if chooser is not None else None

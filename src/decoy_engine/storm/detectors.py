"""PII / format detectors used by STORM.

Each detector is a callable `(series, col_name) -> DetectorMatch | None`. The
profiler runs every registered detector against every column and attaches the
resulting matches to the column's FieldStats.

A detector returns:
    - DetectorMatch if (match_rate >= min_match_rate) OR (column name strongly
      hints at the detector). The match_rate field reports the actual fraction
      of non-null values that matched the pattern, regardless of the name hint
      that may have triggered firing.
    - None otherwise.

Conservative thresholds keep the ranked FORECAST output clean: a column with
30% email-like values shouldn't get tagged "email" - that's noise.

Built-in detector set:
  Core PII:  email, ssn, us_phone, us_zip, person_name,
             iso_date, us_date, eu_date
  PCI/GDPR (Item 31 phase 1):  pan, cvv, iban, ipv4
  HIPAA Safe Harbor + clinical (Item 31 phase 3):
    icd10, npi, mrn, url, fax_number, health_plan_id, license_num,
    vehicle_id, device_id, biometric_id
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pandas as pd

from decoy_engine.storm.types import CustomDetectorSpec, DetectorMatch

# ── thresholds ──────────────────────────────────────────────────────────────────────────────
#
# Detection sprint (V1) lowered the no-hint firing floor from 0.7 to 0.5 so
# medium-confidence finds reach the 3-bucket UI ("needs review") instead of
# being silently dropped. Per-bucket bounds:
#   high   = (name_hint AND rate >= 0.45) OR (rate >= 0.75)
#   medium = no_hint AND rate in [0.50, 0.75), or name_hint AND rate in [0.40, 0.45)
#   low    = no_hint AND rate in [0.30, 0.50)   (opt-in; not fired by default)
#
# The low floor is exposed as a constant so the platform can lower the
# firing threshold behind a "weak signals" flag in V1.5 without re-deriving
# the bucket math.

DEFAULT_MIN_MATCH_RATE = 0.5  # 50% of non-null values must match (no hint)
NAME_HINT_MIN_RATE = 0.4  # 40% if the column name strongly hints
LOW_CONFIDENCE_NO_HINT_FLOOR = 0.3  # opt-in floor for surfacing low-confidence finds
HIGH_CONFIDENCE_NO_HINT_FLOOR = 0.75  # content alone, name hint absent
HIGH_CONFIDENCE_WITH_HINT_FLOOR = 0.45  # content alongside a confirming name hint
SAMPLE_MISS_LIMIT = 3


# ── helpers ───────────────────────────────────────────────────────────────────────────────


def _series_str(series: pd.Series) -> pd.Series:
    """Drop nulls and coerce to string - every detector needs this prelude."""
    return series.dropna().astype(str).str.strip()


def _evaluate(
    detector_id: str,
    values: pd.Series,
    pattern: re.Pattern[str],
    *,
    name_hint: bool,
    min_rate: float,
    validator: Callable[[str], bool] | None = None,
    format_variants: list[tuple[str, re.Pattern[str]]] | None = None,
) -> DetectorMatch | None:
    """Apply a regex to non-null values and decide whether the detector fires.

    Optional `validator` runs per-value AFTER regex match - used for
    structurally-valid PAN (Luhn checksum), IBAN (mod-97), IPv4
    (octet range 0-255), NPI (CMS check digit), and ICD-10 (structure check).
    When set, a value counts as a match only if the regex matches AND
    validator(value) returns True. Avoids the false-positive case where any
    13-digit string looks like a credit card to the regex.

    ``format_variants`` (Item 65) is an ordered list of
    ``(label, sub_pattern)`` pairs that the detector knows about.
    When supplied, the dominant matching variant's label is written to
    ``DetectorMatch.format_pattern`` so the mask post-pass can splice
    separators back at the right positions. Detectors with no variants
    (email, person_name, etc.) pass ``None`` and the field stays ``None``.
    """
    if len(values) == 0:
        return None
    pattern_matches = values.str.fullmatch(pattern)
    if validator is not None:
        # Only run the validator on regex-passers - far cheaper than
        # iterating every value, and a non-matching string can't pass a
        # checksum either.
        validator_results = pd.Series(False, index=values.index)
        candidates = values[pattern_matches]
        for idx, val in candidates.items():
            try:
                if validator(val):
                    validator_results.at[idx] = True
            except (TypeError, ValueError):
                continue
        matches = validator_results
    else:
        matches = pattern_matches
    rate = float(matches.mean())
    threshold = NAME_HINT_MIN_RATE if name_hint else min_rate
    if rate < threshold:
        return None
    misses = values[~matches].head(SAMPLE_MISS_LIMIT).tolist()
    # Variant bucketing - count which sub-pattern won among the matches.
    format_pattern: str | None = None
    if format_variants:
        matched_values = values[matches]
        format_pattern = _dominant_variant(matched_values, format_variants)
    confidence = _confidence_bucket(rate, name_hint=name_hint)
    return DetectorMatch(
        detector_id=detector_id,
        match_rate=round(rate, 4),
        sample_misses=[str(m) for m in misses],
        format_pattern=format_pattern,
        confidence=confidence,
    )


def _confidence_bucket(rate: float, *, name_hint: bool) -> str:
    """Classify a firing detector's confidence into the 3-bucket model.

    Detection sprint (V1). The UI keys off this label, not match_rate, so
    the bucket boundaries live in one place. See module-level thresholds
    block for the source-of-truth bounds.
    """
    if (
        name_hint and rate >= HIGH_CONFIDENCE_WITH_HINT_FLOOR
    ) or rate >= HIGH_CONFIDENCE_NO_HINT_FLOOR:
        return "high"
    if (not name_hint and rate >= 0.50) or (name_hint and rate >= NAME_HINT_MIN_RATE):
        return "medium"
    return "low"


def _dominant_variant(
    values: pd.Series,
    variants: list[tuple[str, re.Pattern[str]]],
) -> str | None:
    """Return the label of the variant that matches the most values.

    Variants are tested in order; each value is counted under the first
    variant that fullmatches it. Returns the highest-count label, or
    None when no variant matched any value.
    """
    if len(values) == 0:
        return None
    remaining = values
    counts: dict[str, int] = {}
    for label, pat in variants:
        if len(remaining) == 0:
            break
        hit = remaining.str.fullmatch(pat)
        n = int(hit.sum())
        if n > 0:
            counts[label] = counts.get(label, 0) + n
            remaining = remaining[~hit]
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ── name-hint patterns (case-insensitive, matched against the column name) ──
#
# Detection sprint (V1): expanded with US English abbreviation forms drawn
# from real-world health, fintech, and payroll headers. Cross-cultural
# variants (Spanish / French / German / etc.) are V1.5; keeping V1 to a
# single locale keeps the false-positive surface predictable.


def _hint(terms: list[str]) -> re.Pattern[str]:
    """Build a case-insensitive name-hint regex from a list of token strings.

    Each term matches when it appears as a standalone token in the column
    name, where tokens are separated by `_`, `-`, `.`, or string boundaries.
    Order of alternation matters only for performance, not correctness.
    """
    body = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"(?i)^(.*[._-])?({body})([._-].*)?$")


# The 26 built-in detectors' column-name hint patterns live in YAML
# under decoy_engine.storm.name_hints.v1/; the loader returns a flat
# dict {detector_id: [term, ...]} and we apply `_hint(terms)` here to
# build the actual re.Pattern objects. Keeping regex construction in
# this module means there's exactly one implementation; the loader is
# a pure data layer.
#
# Behavior matches the pre-extraction hard-coded dict bit-for-bit;
# this is verified by tests/snapshots/test_name_hints_baseline.py.
# Any YAML edit that changes coverage breaks the snapshot and forces
# an explicit re-baseline commit.
#
# See decoy_engine.storm.name_hints.v1/README.md for the YAML format
# + contribution conventions.
from decoy_engine.storm.name_hints.loader import load_name_hint_terms

_NAME_HINTS: dict[str, re.Pattern[str]] = {
    det_id: _hint(terms) for det_id, terms in load_name_hint_terms().items()
}


# Per-scan additive name-hint extras. The platform reads its
# storm_detector_overrides table at scan start and stashes
# {detector_id: re.Pattern} here via set_name_hint_extras(); the
# detect_* functions consult this via _hits_name_hint().
#
# Additive semantics: a column name matches if EITHER the shipped
# pattern OR the per-scan extras pattern matches. Customers cannot
# disable shipped patterns through this surface (that's a "full
# override" mode deferred to a later sprint if real demand appears).
#
# ContextVar (not module-global) so concurrent scans on the same
# process don't cross-contaminate -- each scan's run_storm() call
# sets its own extras for the duration of that call.
from contextlib import contextmanager
from contextvars import ContextVar

_NAME_HINT_EXTRAS: ContextVar[dict[str, re.Pattern[str]] | None] = ContextVar(
    "_NAME_HINT_EXTRAS",
    default=None,
)


@contextmanager
def name_hint_extras(extras: dict[str, list[str]] | None):
    """Context manager: install per-scan name-hint extras.

    Pass a dict mapping detector_id to a list of additional
    column-name term strings. The terms are compiled with the same
    _hint() regex helper used for the shipped library. Behavior is
    additive: shipped patterns still apply unchanged; extras add
    coverage on top.

    Yields and restores the ContextVar so the caller doesn't have
    to manage the token. Pass None to skip installation (no-op).
    """
    if not extras:
        yield
        return
    compiled = {
        det_id: _hint(list(terms))
        for det_id, terms in extras.items()
        if terms  # skip empty lists silently
    }
    token = _NAME_HINT_EXTRAS.set(compiled if compiled else None)
    try:
        yield
    finally:
        _NAME_HINT_EXTRAS.reset(token)


def _hits_name_hint(detector_id: str, col_name: str) -> bool:
    """True if col_name matches the shipped pattern OR a per-scan extra.

    Checked in this order:
      1. Shipped pattern from the YAML library (_NAME_HINTS).
      2. Per-scan extras installed by name_hint_extras() context.
    Either match returns True; matching is additive.
    """
    target = col_name or ""
    pat = _NAME_HINTS.get(detector_id)
    if pat and pat.fullmatch(target):
        return True
    extras = _NAME_HINT_EXTRAS.get()
    if extras:
        extra_pat = extras.get(detector_id)
        if extra_pat and extra_pat.fullmatch(target):
            return True
    return False


def hits_name_hint(detector_id: str, col_name: str) -> bool:
    """Public accessor used by the profiler to build a column's detection trail.

    Returns True when the column name structurally hints at the detector's
    target type (e.g. column "ssn_id" hints at the SSN detector). The
    threshold-relaxation logic itself stays internal to `_evaluate`.
    """
    return _hits_name_hint(detector_id, col_name)


# ── value patterns ────────────────────────────────────────────────────────────────────────────

# Email - RFC 5321ish but not strict; works for the 99% of fields users feed in.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# SSN - ###-##-#### or 9 consecutive digits. Reject 000-/666-/9##- per SSA rules.
_SSN_RE = re.compile(r"(?!000|666|9\d{2})\d{3}-?(?!00)\d{2}-?(?!0000)\d{4}")

# US phone - 10 digits with common separators, optional +1 country code.
_US_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?[2-9]\d{2}[\s.-]?\d{4}")

# US ZIP -- 5 digits, optional -#### extension.
# QA-3 F6 (2026-05-31): non-word lookbehind + lookahead reject 5-digit
# numbers inside larger numeric tokens like "12345mg" or
# "weight: 12345.6" (a dose or weight reading). Pre-fix `\d{5}` matched
# as a substring, so any 5-digit block in a dose/measurement column
# over-fired the us_zip detector and the column got pulled into the
# PII span set. `\b` alone is insufficient because `.` is a non-word
# character, so "12345.6" still satisfies \b at the boundary; the
# extra (?!\.\d) lookahead rejects the decimal-number case.
_US_ZIP_RE = re.compile(r"(?<!\w)\d{5}(?:-\d{4})?(?!\w)(?!\.\d)")

# Date formats - strict patterns; the profiler also has pandas' to_datetime
# fuzzy parser as a backstop. These are for *format signal* only.
#
# ISO date accepts both the dashed shape (YYYY-MM-DD with optional time
# component) AND the compact 8-digit shape (YYYYMMDD). The compact
# branch is gated by `_iso_compact_date_valid` so a random 8-digit
# ID column doesn't false-positive as a date.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?Z?|\d{8}")
_US_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
_EU_DATE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{2,4}|\d{1,2}-\d{1,2}-\d{4}")

# Person name - 1-3 whitespace-separated tokens, each starts with a letter,
# letters / hyphens / apostrophes / dots only. Length 2-50 total.
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z''.\-]{0,29}")
_PERSON_NAME_RE = re.compile(rf"{_NAME_TOKEN_RE.pattern}(?:\s+{_NAME_TOKEN_RE.pattern}){{0,2}}")

# PAN (credit card) - 13-19 digits with optional spaces or dashes between
# groups of 4. Final validity check is Luhn (mod-10) - the regex alone
# false-positives on any 13+ digit number, which is far too noisy.
_PAN_RE = re.compile(r"\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{1,7}")

# CVV - 3 or 4 digits. Pure regex match is uselessly broad (any 3-digit
# string), so this detector only fires on a strong column-name hint.
_CVV_RE = re.compile(r"\d{3,4}")

# IBAN - 2-letter country code + 2-digit checksum + 11-30 alphanumerics.
# Spaces optional, often grouped in 4s. Final validity check is mod-97.
_IBAN_RE = re.compile(r"[A-Z]{2}\d{2}[\sA-Z0-9]{11,34}")

# IPv4 - four 1-3 digit octets separated by dots. Range check (each octet
# 0-255) is the per-value validator.
_IPV4_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")

# ICD-10-CM - chapter letter + 2-digit category + optional decimal subcategory.
# Examples: A01.0, M79.3, S72.001A, Z23, F32.9, A010 (stored without dot).
_ICD10_RE = re.compile(r"[A-Z]\d{2}(?:\.?[A-Z0-9]{1,4})?", re.IGNORECASE)

# NPI - exactly 10 digits; check digit validated by CMS Luhn variant.
_NPI_RE = re.compile(r"\d{10}")

# MRN - no universal format; institution-defined alphanumeric + dash, 4-20 chars.
# Name-hint is the primary signal; this pattern guards against non-identifier noise.
_MRN_RE = re.compile(r"[A-Z0-9\-]{4,20}", re.IGNORECASE)

# URL - http/https scheme with a host and optional path.
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]{4,}")

# Fax number - identical value pattern to US phone; name hint is the
# disambiguation signal (phone vs fax).
_FAX_NUMBER_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?[2-9]\d{2}[\s.-]?\d{4}")

# Name-hint-only detectors - patterns broad enough to match any plausible
# identifier value; meaning lives in the column name, not the value shape.
_HEALTH_PLAN_ID_RE = re.compile(r"[A-Z0-9\-]{4,30}", re.IGNORECASE)
_LICENSE_NUM_RE = re.compile(r"[A-Z0-9\-]{4,20}", re.IGNORECASE)

# VIN - exactly 17 chars with restricted charset (no I, O, or Q per ISO 3779).
_VEHICLE_ID_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}", re.IGNORECASE)

_DEVICE_ID_RE = re.compile(r"[A-Z0-9\-_.]{4,30}", re.IGNORECASE)
_BIOMETRIC_ID_RE = re.compile(r".+")  # any non-empty value; name hint is definitive

# Address - number + street word(s). Loose by design; the column-name hint
# carries the meaning and the value pattern just filters out obvious non-
# addresses (pure phone numbers, dates).
_ADDRESS_RE = re.compile(
    r"\d+\s+[A-Za-z][A-Za-z0-9\s,.\-#'/]+",
    re.IGNORECASE,
)


# ── format variants (Item 65) ───────────────────────────────────────────────────────────────
#
# Per-detector ordered (label, regex) pairs that classify which sub-shape
# of the detector's parent pattern actually fired. The label is what the
# mask post-pass reads - for regex-style detectors (SSN, phone, ZIP) it's
# the regex shape; for date detectors it's a strptime format string the
# date_shift strategy can pass directly to dt.strftime().


def _variant(label: str) -> tuple[str, re.Pattern[str]]:
    """F-8 fix: derive the compiled regex from the label so the two cannot
    drift. Used by the regex-style variants where ``label == regex_source``.
    The date variants below intentionally pass an independent label
    (a strptime format string) so they cannot use this helper."""
    return label, re.compile(label)


_SSN_VARIANTS = [
    _variant(r"\d{3}-\d{2}-\d{4}"),
    _variant(r"\d{9}"),
]

_US_PHONE_VARIANTS = [
    # NB: most distinctive shapes first so e.g. "(NNN) NNN-NNNN" doesn't
    # leak into the bare-dash bucket.
    _variant(r"\(\d{3}\) \d{3}-\d{4}"),
    _variant(r"\(\d{3}\)\d{3}-\d{4}"),
    _variant(r"\d{3}-\d{3}-\d{4}"),
    _variant(r"\d{3}\.\d{3}\.\d{4}"),
    _variant(r"\d{3} \d{3} \d{4}"),
    _variant(r"\+1 \d{3} \d{3} \d{4}"),
    _variant(r"\+1-\d{3}-\d{3}-\d{4}"),
    _variant(r"\d{10}"),
]

_US_ZIP_VARIANTS = [
    _variant(r"\d{5}-\d{4}"),
    # QA-3 F6 (2026-05-31): the bare 5-digit variant gets the same
    # non-word boundary protection as the iter_spans pattern; otherwise
    # the format-variant rate calculation accepts any 5-digit substring
    # in a measurement column as a us_zip match.
    _variant(r"(?<!\w)\d{5}(?!\w)(?!\.\d)"),
]

# Date detectors map directly to strptime - the format_pattern label is
# what date_shift's dt.strftime() will consume on the masked output.
_ISO_DATE_VARIANTS = [
    ("%Y-%m-%dT%H:%M:%SZ", re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")),
    ("%Y-%m-%dT%H:%M:%S", re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")),
    ("%Y-%m-%d %H:%M:%S", re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")),
    ("%Y-%m-%dT%H:%M", re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")),
    ("%Y-%m-%d %H:%M", re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")),
    ("%Y-%m-%d", re.compile(r"\d{4}-\d{2}-\d{2}")),
    ("%Y%m%d", re.compile(r"\d{8}")),
]
_US_DATE_VARIANTS = [
    ("%m/%d/%Y", re.compile(r"\d{1,2}/\d{1,2}/\d{4}")),
    ("%m/%d/%y", re.compile(r"\d{1,2}/\d{1,2}/\d{2}")),
]
_EU_DATE_VARIANTS = [
    ("%d.%m.%Y", re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}")),
    ("%d.%m.%y", re.compile(r"\d{1,2}\.\d{1,2}\.\d{2}")),
    ("%d-%m-%Y", re.compile(r"\d{1,2}-\d{1,2}-\d{4}")),
]

_PAN_VARIANTS = [
    _variant(r"\d{4} \d{4} \d{4} \d{4}"),
    _variant(r"\d{4}-\d{4}-\d{4}-\d{4}"),
    _variant(r"\d{16}"),
    _variant(r"\d{15}"),  # Amex
    _variant(r"\d{14}"),  # Diners
]

_ICD10_VARIANTS = [
    _variant(r"[A-Z]\d{2}\.[A-Z0-9]{1,4}"),
    _variant(r"[A-Z]\d{2}"),
    _variant(r"[A-Z]\d{2}[A-Z0-9]{1,4}"),
]


# ── validators ──────────────────────────────────────────────────────────────────────────────


def _luhn_valid(value: str) -> bool:
    """Luhn / mod-10 checksum used by every major credit-card scheme.
    Strips spaces and dashes; rejects anything that isn't pure digits
    after stripping. Lower bound on length (13) keeps it from accepting
    very short numbers that happen to satisfy the checksum."""
    digits = re.sub(r"[\s-]", "", value)
    if not digits.isdigit() or len(digits) < 13:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ISO 3166-1 alpha-2 codes of countries that issue IBANs (SWIFT registry as of
# 2024). Detection sprint (V1) gates the IBAN validator on country membership
# so random "GB"- or "DE"-prefixed alphanumerics don't pass the mod-97 check
# by luck. Adding a new country (e.g. when SWIFT publishes the next update)
# is a one-line edit here.
_IBAN_COUNTRIES: frozenset[str] = frozenset(
    {
        "AD",
        "AE",
        "AL",
        "AT",
        "AZ",
        "BA",
        "BE",
        "BG",
        "BH",
        "BR",
        "BY",
        "CH",
        "CR",
        "CY",
        "CZ",
        "DE",
        "DK",
        "DO",
        "EE",
        "EG",
        "ES",
        "FI",
        "FO",
        "FR",
        "GB",
        "GE",
        "GI",
        "GL",
        "GR",
        "GT",
        "HR",
        "HU",
        "IE",
        "IL",
        "IQ",
        "IS",
        "IT",
        "JO",
        "KW",
        "KZ",
        "LB",
        "LC",
        "LI",
        "LT",
        "LU",
        "LV",
        "MC",
        "MD",
        "ME",
        "MK",
        "MR",
        "MT",
        "MU",
        "NL",
        "NO",
        "PK",
        "PL",
        "PS",
        "PT",
        "QA",
        "RO",
        "RS",
        "RU",
        "SA",
        "SC",
        "SE",
        "SI",
        "SK",
        "SM",
        "ST",
        "SV",
        "TL",
        "TN",
        "TR",
        "UA",
        "VA",
        "VG",
        "XK",
    }
)


def _iban_valid(value: str) -> bool:
    """ISO 13616 mod-97 check, gated by ISO 3166 country-code membership.

    After stripping spaces and uppercasing, the first two characters must
    be a known IBAN-issuing country code; that filter rejects random
    alphanumeric strings that happen to satisfy mod-97. Then move the
    first 4 chars to the end, replace letters with digits (A=10, B=11,
    …, Z=35), and verify integer mod 97 == 1.
    """
    iban = re.sub(r"\s", "", str(value).upper())
    if len(iban) < 15 or len(iban) > 34:
        return False
    if not (iban[:2].isalpha() and iban[2:4].isdigit()):
        return False
    if iban[:2] not in _IBAN_COUNTRIES:
        return False
    rearranged = iban[4:] + iban[:4]
    digits = []
    for c in rearranged:
        if c.isdigit():
            digits.append(c)
        elif c.isalpha():
            digits.append(str(ord(c) - 55))
        else:
            return False
    try:
        return int("".join(digits)) % 97 == 1
    except ValueError:
        return False


def _ipv4_valid(value: str) -> bool:
    parts = str(value).split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or len(p) > 3:
            return False
        n = int(p)
        if n < 0 or n > 255:
            return False
    return True


def _npi_valid(value: str) -> bool:
    """CMS NPI check digit: prepend '80840' to the 9-digit NPI body, apply
    a modified Luhn (even 0-indexed positions from right are doubled), verify
    the computed check digit matches NPI[9].

    Verified: 1234567893 -> prefix 80840123456789 -> sum 67 -> check 3 ✓
              1679576722 -> prefix 80840167957672 -> sum 68 -> check 2 ✓
              1000000004 -> prefix 80840100000000 -> sum 26 -> check 4 ✓
    """
    digits = re.sub(r"[\s-]", "", str(value))
    if not digits.isdigit() or len(digits) != 10:
        return False
    prefixed = "80840" + digits[:9]
    total = 0
    for i, ch in enumerate(reversed(prefixed)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(digits[9])


def _iso_date_valid(value: str) -> bool:
    """Reject random 8-digit strings that pass the compact-date branch
    but aren't plausible dates. Year 1900-2100, month 1-12, day 1-31.
    Dashed dates always pass - only the compact branch needs the guard."""
    v = value.strip()
    if "-" in v or "T" in v or " " in v:
        return True
    if len(v) != 8 or not v.isdigit():
        return False
    year = int(v[:4])
    month = int(v[4:6])
    day = int(v[6:8])
    return 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


# ICD-10-CM chapter category ranges. Each entry maps a chapter letter to
# the inclusive [lo, hi] 2-digit category range that's actually used in
# the standard. Detection sprint (V1) uses this to gate the icd10
# validator so a random "M99" or "Z45" string can't false-positive - the
# letter+category prefix must be in a real ICD-10 chapter range.
#
# A bundled top-1000 lookup would be more precise (rejecting structurally
# valid but non-existent codes like J50.0); that's tracked in the gap doc
# as a V1.5 item alongside the cross-cultural patterns.
_ICD10_CHAPTERS: dict[str, tuple[int, int]] = {
    "A": (0, 99),
    "B": (0, 99),  # infectious + parasitic
    "C": (0, 99),
    "D": (0, 89),  # neoplasms / blood (D50-D89 separate chapter but same letter)
    "E": (0, 89),  # endocrine
    "F": (1, 99),  # mental
    "G": (0, 99),  # nervous
    "H": (0, 95),  # eye + ear
    "I": (0, 99),  # circulatory
    "J": (0, 99),  # respiratory
    "K": (0, 95),  # digestive
    "L": (0, 99),  # skin
    "M": (0, 99),  # musculoskeletal
    "N": (0, 99),  # genitourinary
    "O": (0, 99),  # pregnancy (O9A handled separately)
    "P": (0, 96),  # perinatal
    "Q": (0, 99),  # congenital
    "R": (0, 99),  # symptoms / signs
    "S": (0, 99),
    "T": (0, 88),  # injury / poisoning
    "U": (0, 85),  # special purposes (COVID-19)
    "V": (0, 99),
    "W": (0, 99),  # external causes
    "X": (0, 99),
    "Y": (0, 99),
    "Z": (0, 99),  # factors influencing health
}


def _icd10_valid(value: str) -> bool:
    """ICD-10-CM structural + chapter-range validity.

    Verified: letter at index 0 belongs to a real ICD-10 chapter; the 2-digit
    category prefix falls within that chapter's valid range; total length
    3-7 alphanumeric characters (dots stripped). Rejects e.g. "Z99.99X" -> fine,
    "P97.00" -> P97 outside P0-P96 -> rejected.
    """
    v = re.sub(r"\.", "", str(value).strip().upper())
    if not (3 <= len(v) <= 7 and v[0].isalpha() and v[1:3].isdigit()):
        return False
    chapter = v[0]
    if chapter not in _ICD10_CHAPTERS:
        return False
    cat_lo, cat_hi = _ICD10_CHAPTERS[chapter]
    cat = int(v[1:3])
    return cat_lo <= cat <= cat_hi


# ── detectors (callables) ───────────────────────────────────────────────────────────────────


def detect_email(series: pd.Series, col_name: str) -> DetectorMatch | None:
    return _evaluate(
        "email",
        _series_str(series),
        _EMAIL_RE,
        name_hint=_hits_name_hint("email", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
    )


def detect_ssn(series: pd.Series, col_name: str) -> DetectorMatch | None:
    return _evaluate(
        "ssn",
        _series_str(series),
        _SSN_RE,
        name_hint=_hits_name_hint("ssn", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        format_variants=_SSN_VARIANTS,
    )


def detect_us_phone(series: pd.Series, col_name: str) -> DetectorMatch | None:
    return _evaluate(
        "us_phone",
        _series_str(series),
        _US_PHONE_RE,
        name_hint=_hits_name_hint("us_phone", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        format_variants=_US_PHONE_VARIANTS,
    )


def detect_us_zip(series: pd.Series, col_name: str) -> DetectorMatch | None:
    return _evaluate(
        "us_zip",
        _series_str(series),
        _US_ZIP_RE,
        name_hint=_hits_name_hint("us_zip", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        format_variants=_US_ZIP_VARIANTS,
    )


def detect_person_name(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Heuristic: name-hinted column name + values look like proper-cased tokens.

    Without a strong column-name hint, person_name is too noisy - most short
    string columns happen to "look like names" by this regex. Require the hint.
    """
    if not _hits_name_hint("person_name", col_name):
        return None
    return _evaluate(
        "person_name",
        _series_str(series),
        _PERSON_NAME_RE,
        name_hint=True,
        min_rate=DEFAULT_MIN_MATCH_RATE,
    )


def detect_first_name(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """First-name columns (fn, f_name, firstname, mm_fn, cust_fn, ...).

    Detection sprint (V1): split out from person_name so the strategy
    table routes to faker.first_name (single token, gendered shape)
    instead of faker.name. Name-hint only - value regex is the same as
    person_name.
    """
    if not _hits_name_hint("first_name", col_name):
        return None
    return _evaluate(
        "first_name",
        _series_str(series),
        _PERSON_NAME_RE,
        name_hint=True,
        min_rate=NAME_HINT_MIN_RATE,
    )


def detect_last_name(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Last-name columns (ln, l_name, lastname, surname, mm_ln, cust_ln, ...).

    Detection sprint (V1) sibling of detect_first_name. Routes to
    faker.last_name in the strategy table.
    """
    if not _hits_name_hint("last_name", col_name):
        return None
    return _evaluate(
        "last_name",
        _series_str(series),
        _PERSON_NAME_RE,
        name_hint=True,
        min_rate=NAME_HINT_MIN_RATE,
    )


def detect_address(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Street addresses (addr, addr1, line1, street_1, mailing_address, ...).

    Detection sprint (V1). Name-hint only - street formats vary too widely
    (rural routes, military APO, PO boxes, international) for a meaningful
    value regex. The address regex just filters out obviously non-address
    values (pure phone numbers, plain prose without a leading digit).
    """
    if not _hits_name_hint("address", col_name):
        return None
    return _evaluate(
        "address", _series_str(series), _ADDRESS_RE, name_hint=True, min_rate=NAME_HINT_MIN_RATE
    )


def detect_iso_date(series: pd.Series, col_name: str) -> DetectorMatch | None:
    return _evaluate(
        "iso_date",
        _series_str(series),
        _ISO_DATE_RE,
        name_hint=_hits_name_hint("iso_date", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        validator=_iso_date_valid,
        format_variants=_ISO_DATE_VARIANTS,
    )


def detect_us_date(series: pd.Series, col_name: str) -> DetectorMatch | None:
    return _evaluate(
        "us_date",
        _series_str(series),
        _US_DATE_RE,
        name_hint=_hits_name_hint("us_date", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        format_variants=_US_DATE_VARIANTS,
    )


def detect_eu_date(series: pd.Series, col_name: str) -> DetectorMatch | None:
    return _evaluate(
        "eu_date",
        _series_str(series),
        _EU_DATE_RE,
        name_hint=_hits_name_hint("eu_date", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        format_variants=_EU_DATE_VARIANTS,
    )


def detect_pan(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Credit-card PAN. Regex picks up 13-19-digit groups; Luhn checksum
    rejects random digit strings so a 16-digit phone stub or transaction
    ID doesn't false-positive."""
    return _evaluate(
        "pan",
        _series_str(series),
        _PAN_RE,
        name_hint=_hits_name_hint("pan", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        validator=_luhn_valid,
        format_variants=_PAN_VARIANTS,
    )


def detect_cvv(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """CVV / CVC. Any 3-digit string matches the regex, so the detector
    only fires on a strong column-name hint - false-positive rate
    without the hint would be unmanageable."""
    if not _hits_name_hint("cvv", col_name):
        return None
    return _evaluate(
        "cvv", _series_str(series), _CVV_RE, name_hint=True, min_rate=DEFAULT_MIN_MATCH_RATE
    )


def detect_iban(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """IBAN - country code + checksum + BBAN. Mod-97 validates the
    checksum so random alphanumeric strings don't false-positive."""
    return _evaluate(
        "iban",
        _series_str(series),
        _IBAN_RE,
        name_hint=_hits_name_hint("iban", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        validator=_iban_valid,
    )


def detect_ipv4(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """IPv4 dotted-quad. Validator clamps each octet to 0-255 so
    "999.1.1.1" is rejected even though it matches the regex."""
    return _evaluate(
        "ipv4",
        _series_str(series),
        _IPV4_RE,
        name_hint=_hits_name_hint("ipv4", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        validator=_ipv4_valid,
    )


def detect_icd10(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """ICD-10-CM diagnosis codes. Regex matches the chapter-letter + 2-digit
    category structure; validator enforces minimal structural rules to reduce
    false-positives on short arbitrary strings. Fires on value pattern alone;
    name hint lowers the threshold."""
    return _evaluate(
        "icd10",
        _series_str(series),
        _ICD10_RE,
        name_hint=_hits_name_hint("icd10", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        validator=_icd10_valid,
        format_variants=_ICD10_VARIANTS,
    )


def detect_npi(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """National Provider Identifier - 10-digit with CMS Luhn check digit."""
    return _evaluate(
        "npi",
        _series_str(series),
        _NPI_RE,
        name_hint=_hits_name_hint("npi", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
        validator=_npi_valid,
    )


def detect_mrn(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Medical Record Number - no universal format; fires on name hint only.
    The alphanumeric pattern guards against obviously non-identifier values
    (plain prose, floats, very short strings)."""
    if not _hits_name_hint("mrn", col_name):
        return None
    return _evaluate(
        "mrn", _series_str(series), _MRN_RE, name_hint=True, min_rate=NAME_HINT_MIN_RATE
    )


def detect_url(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Web URLs - http/https scheme. Fires on value pattern alone (no
    name hint required) since the URL format is distinctive enough."""
    return _evaluate(
        "url",
        _series_str(series),
        _URL_RE,
        name_hint=_hits_name_hint("url", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
    )


def detect_fax_number(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Fax numbers - identical format to US phone; name hint is the only
    way to distinguish a fax column from a phone column."""
    if not _hits_name_hint("fax_number", col_name):
        return None
    return _evaluate(
        "fax_number",
        _series_str(series),
        _FAX_NUMBER_RE,
        name_hint=True,
        min_rate=DEFAULT_MIN_MATCH_RATE,
    )


def detect_health_plan_id(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Health-plan beneficiary / member / subscriber IDs - name hint only.
    No standard value format; meaning lives in the column name."""
    if not _hits_name_hint("health_plan_id", col_name):
        return None
    return _evaluate(
        "health_plan_id",
        _series_str(series),
        _HEALTH_PLAN_ID_RE,
        name_hint=True,
        min_rate=NAME_HINT_MIN_RATE,
    )


def detect_license_num(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Certificate and license numbers - name hint only. Formats vary by
    state / regulatory body; the column name is the definitive signal."""
    if not _hits_name_hint("license_num", col_name):
        return None
    return _evaluate(
        "license_num",
        _series_str(series),
        _LICENSE_NUM_RE,
        name_hint=True,
        min_rate=NAME_HINT_MIN_RATE,
    )


def detect_vehicle_id(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Vehicle identifiers. The VIN format (17 alphanum, no I/O/Q per ISO 3779)
    fires without a name hint; name hint lowers the threshold for partial or
    non-VIN vehicle identifiers."""
    return _evaluate(
        "vehicle_id",
        _series_str(series),
        _VEHICLE_ID_RE,
        name_hint=_hits_name_hint("vehicle_id", col_name),
        min_rate=DEFAULT_MIN_MATCH_RATE,
    )


def detect_device_id(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Device identifiers and serial numbers - name hint only. Medical device
    UDIs, implant serial numbers, and equipment IDs have no shared format."""
    if not _hits_name_hint("device_id", col_name):
        return None
    return _evaluate(
        "device_id", _series_str(series), _DEVICE_ID_RE, name_hint=True, min_rate=NAME_HINT_MIN_RATE
    )


def detect_biometric_id(series: pd.Series, col_name: str) -> DetectorMatch | None:
    """Biometric identifiers (fingerprints, retina scans, etc.) - name hint only.
    Biometric data has no universal string format; the column name is definitive."""
    if not _hits_name_hint("biometric_id", col_name):
        return None
    return _evaluate(
        "biometric_id",
        _series_str(series),
        _BIOMETRIC_ID_RE,
        name_hint=True,
        min_rate=NAME_HINT_MIN_RATE,
    )


# ── registry ──────────────────────────────────────────────────────────────────────────────────

DetectorFn = Callable[[pd.Series, str], DetectorMatch | None]

REGISTERED_DETECTORS: list[DetectorFn] = [
    detect_email,
    detect_ssn,
    detect_us_phone,
    detect_us_zip,
    # Specific name detectors before the generic person_name so the
    # strategy table routes to faker.first_name / faker.last_name when the
    # column header is specific.
    detect_first_name,
    detect_last_name,
    detect_person_name,
    detect_address,
    detect_iso_date,
    detect_us_date,
    detect_eu_date,
    detect_pan,
    detect_cvv,
    detect_iban,
    detect_ipv4,
    # Item 31 phase 3 - HIPAA Safe Harbor completers + clinical identifiers.
    detect_icd10,
    detect_npi,
    detect_mrn,
    detect_url,
    detect_fax_number,
    detect_health_plan_id,
    detect_license_num,
    detect_vehicle_id,
    detect_device_id,
    detect_biometric_id,
]


def run_all_detectors(
    series: pd.Series,
    col_name: str,
    *,
    custom: list[CustomDetectorSpec] | None = None,
) -> list[DetectorMatch]:
    """Run every registered detector against a column, plus any caller-supplied
    custom detectors. Returns matches sorted by descending match_rate so the
    highest-confidence detector is first.

    Custom detectors run after the built-ins. A bad regex in one custom spec
    (e.g. malformed pattern) is logged-and-skipped rather than raising - one
    misconfigured admin entry shouldn't kill the whole scan.
    """
    matches: list[DetectorMatch] = []
    for fn in REGISTERED_DETECTORS:
        m = fn(series, col_name)
        if m is not None:
            matches.append(m)
    if custom:
        for spec in custom:
            try:
                m = _run_custom_detector(series, col_name, spec)
            except re.error:
                # Bad regex - skip silently. The platform validates patterns
                # at create-time so we should rarely hit this in practice.
                continue
            if m is not None:
                matches.append(m)
    matches.sort(key=lambda m: m.match_rate, reverse=True)
    return matches


# ── custom detectors ──────────────────────────────────────────────────────────────────────────


def _custom_name_hint_pattern(name_hints: list[str]) -> re.Pattern[str] | None:
    """Compile a column-name-matching regex from a list of substrings.

    Mirrors the built-in `_NAME_HINTS` shape: case-insensitive, matches when
    any hint appears as a token in the column name (separated by `_-` or at
    word boundaries). Empty/whitespace hints are skipped.
    """
    cleaned = [re.escape(h.strip()) for h in name_hints if h.strip()]
    if not cleaned:
        return None
    body = "|".join(cleaned)
    return re.compile(rf"(?i)(^|[._-])({body})($|[._-])")


def _hits_custom_name_hint(spec: CustomDetectorSpec, col_name: str) -> bool:
    pat = _custom_name_hint_pattern(spec.name_hints)
    return bool(pat and pat.search(col_name or ""))


def _run_custom_detector(
    series: pd.Series,
    col_name: str,
    spec: CustomDetectorSpec,
) -> DetectorMatch | None:
    """Compile + apply one custom regex spec to a column."""
    pattern = re.compile(spec.pattern)
    name_hint = _hits_custom_name_hint(spec, col_name)
    return _evaluate(
        spec.id,
        _series_str(series),
        pattern,
        name_hint=name_hint,
        min_rate=max(0.0, min(1.0, spec.threshold)),
    )


def hits_custom_name_hint(spec: CustomDetectorSpec, col_name: str) -> bool:
    """Public accessor used by the profiler to log name-hint trail rows for
    custom detectors. Mirrors `hits_name_hint` for built-ins."""
    return _hits_custom_name_hint(spec, col_name)


# ── iter_spans: in-text PII span extraction (MG-2, 2026-05-31) ────────────────
#
# Surfaces the existing detector regexes under a span-iterating contract so
# the `text_redact` masking strategy (and future text-aware strategies) can
# replace PII spans inside free-text columns without rebuilding the regex
# catalog. Additive only: no existing detector behavior changes.
#
# Public surface:
#   - `Span` dataclass (detector_id, start, end, matched_text)
#   - `iter_spans(text, detector_ids=None, *, custom=None) -> list[Span]`
#
# Overlap policy: leftmost-then-longest. Sort by (start, -length) and keep
# the first span; drop any subsequent span whose [start, end) overlaps a
# kept span. Priority-based resolution (e.g. SSN beats person_name) is a
# fast-follow if customers ask.

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """A non-overlapping PII span inside a text cell.

    `detector_id` matches the keys in `_SPAN_DETECTORS` (``"email"``,
    ``"ssn"``, ``"us_phone"``, ``"pan"``, ``"npi"``, ``"icd10"``,
    ``"iban"``, etc).
    """

    detector_id: str
    start: int
    end: int
    matched_text: str


# Map from detector_id to (compiled_regex, optional_validator). Validators
# are the same per-value functions that `_evaluate` already calls; reusing
# them here keeps the column-level + span-level detector behavior in sync.
# Mirrors the existing module-level `_*_RE` constants.
#
# `person_name` / `address` / name-hint-only detectors (license_num,
# health_plan_id, device_id, biometric_id, vehicle_id, mrn) are intentionally
# OMITTED: their regexes are too loose to use without a column-name signal
# (per the existing docstrings on each). The text_redact contract is "find
# unambiguous PII inside free text" - adding name-hint-only detectors here
# would shred legitimate prose. Customers needing those detectors in text
# pass them via the `custom` kwarg with a tighter pattern.
_SPAN_DETECTORS: dict[str, tuple[re.Pattern[str], Callable[[str], bool] | None]] = {
    "email": (_EMAIL_RE, None),
    "ssn": (_SSN_RE, None),
    "us_phone": (_US_PHONE_RE, None),
    "us_zip": (_US_ZIP_RE, None),
    "pan": (_PAN_RE, _luhn_valid),
    "iban": (_IBAN_RE, _iban_valid),
    "ipv4": (_IPV4_RE, _ipv4_valid),
    "icd10": (_ICD10_RE, _icd10_valid),
    "npi": (_NPI_RE, _npi_valid),
    "url": (_URL_RE, None),
}


def iter_spans(
    text: str,
    detector_ids: list[str] | None = None,
    *,
    custom: list[dict] | None = None,
) -> list[Span]:
    """Yield non-overlapping PII spans found in ``text``.

    ``detector_ids`` selects which built-in detectors run; ``None`` runs every
    detector in `_SPAN_DETECTORS`. Unknown detector ids are silently skipped
    (matches the platform's per-tenant detector-overrides shape, where the
    same id might be present on one tenant and absent on another).

    ``custom`` is an optional list of one-shot detector specs of shape
    ``{"detector_id": str, "pattern": re.Pattern, "validator": callable | None}``.
    Custom detectors run after the built-ins and are subject to the same
    overlap dedupe.

    Returns spans sorted by ``.start``, with overlaps resolved by keeping
    the leftmost-then-longest match.
    """
    if not isinstance(text, str) or not text:
        return []
    if detector_ids is None:
        detector_ids = list(_SPAN_DETECTORS.keys())

    raw_spans: list[Span] = []
    for det_id in detector_ids:
        entry = _SPAN_DETECTORS.get(det_id)
        if entry is None:
            continue
        regex, validator = entry
        for m in regex.finditer(text):
            matched = m.group(0)
            if validator is not None and not validator(matched):
                continue
            raw_spans.append(Span(det_id, m.start(), m.end(), matched))

    for spec in custom or []:
        pattern = spec["pattern"]
        validator = spec.get("validator")
        det_id = spec["detector_id"]
        for m in pattern.finditer(text):
            matched = m.group(0)
            if validator is not None and not validator(matched):
                continue
            raw_spans.append(Span(det_id, m.start(), m.end(), matched))

    raw_spans.sort(key=lambda s: (s.start, -(s.end - s.start)))
    out: list[Span] = []
    last_end = -1
    for s in raw_spans:
        if s.start >= last_end:
            out.append(s)
            last_end = s.end

    return out

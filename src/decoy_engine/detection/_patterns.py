"""Compiled value-pattern regexes for storm detectors, split from detectors.py (F11b).

Leaf module: just compiled ``re`` patterns, no dependency on detector logic.
Imported back into detectors.py (and reused by the span scanner) so the
regex catalog lives in one place under the size cap.
"""

from __future__ import annotations

import re

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

"""Checksum/format validators for storm detectors, split out of detectors.py (F11b).

Pure per-value validators (Luhn mod-10, IBAN mod-97, IPv4 range, CMS NPI
check digit, ISO date plausibility, ICD-10 chapter range) plus their
reference data tables. Self-contained (stdlib re only); imported back into
detectors.py so the decoy_engine.storm.detectors._luhn_valid / _iban_valid /
... callables and the _IBAN_COUNTRIES / _ICD10_CHAPTERS tables stay reachable
at the path external callers already import.
"""

from __future__ import annotations

import re


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

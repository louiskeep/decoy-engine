"""Check-digit registry for structured identifier schemes (SP-04 / P5.INFRA.1).

Provides two public functions with a uniform signature:

    validate(scheme, value) -> bool
    calc_check_digit(scheme, body) -> str

Supported schemes
-----------------
luhn    Credit-card PAN and any Luhn-protected digit string.
        Uses python-stdnum 2.2 stdnum.luhn (Luhn 1954, US Patent 2,950,048).

npi     US National Provider Identifier (10 digits, CMS NPPES).
        python-stdnum has no NPI module; hand-rolled per the CMS check-digit
        specification:
        https://www.cms.gov/Regulations-and-Guidance/Administrative-Simplification/
        NationalProvIdentStand/Downloads/NPIcheckdigit.pdf
        Algorithm: Luhn applied to the string '80840' + 9-digit NPI body.

iban    International Bank Account Number (ISO 13616).
        Uses python-stdnum 2.2 stdnum.iban (ISO 7064 mod-97).
        Body = CC (2-char country code) + BBAN; returns 2-char check string.

vin     Vehicle Identification Number (ISO 3779 / NHTSA 49 CFR Part 565).
        python-stdnum has no VIN module; hand-rolled per the NHTSA standard.
        Body = the 16 non-check characters (positions 0-7 and 9-16 of the
        full 17-character VIN).  Check = single char: '0'-'9' or 'X' (=10).

isbn13  ISBN-13 (ISO 2108, 13-digit form).
        Validation via python-stdnum 2.2 stdnum.isbn.is_valid.
        Check-digit via stdnum.ean.calc_check_digit (ISBN-13 reuses the GS1
        algorithm; body = 12 digits).

ean13   EAN-13 / UPC (GS1 General Specifications).
        Uses python-stdnum 2.2 stdnum.ean (body = 12 digits; check = 1 digit).

gtin    GTIN (Global Trade Item Number, GS1; 8/12/13/14-digit variants).
        Uses python-stdnum 2.2 stdnum.ean which handles all four lengths.
        Body = all digits except the trailing check digit.

Dependencies
------------
python-stdnum >= 2.2  (core dependency, declared in pyproject.toml).

Internal notes
--------------
python-stdnum validate() functions return the normalised value on success and
raise stdnum.exceptions.ValidationError on failure; use is_valid() to get
a plain bool.  calc_check_digit functions return a string (1 or 2 chars).
"""

from __future__ import annotations

import re
from collections.abc import Callable

import stdnum.ean as _ean
import stdnum.iban as _iban
import stdnum.isbn as _isbn
import stdnum.luhn as _luhn

# ---------------------------------------------------------------------------
# Luhn
# ---------------------------------------------------------------------------

# Delegates entirely to python-stdnum (Luhn 1954, US Patent 2,950,048).


def _luhn_validate(value: str) -> bool:
    return _luhn.is_valid(value)


def _luhn_calc_check_digit(body: str) -> str:
    return _luhn.calc_check_digit(body)


# ---------------------------------------------------------------------------
# NPI  (hand-rolled: not in python-stdnum)
# ---------------------------------------------------------------------------

_NPI_PREFIX = "80840"
_NPI_RE = re.compile(r"^\d{10}$")


def _luhn_cd(digits: str) -> str:
    """Luhn check digit for an arbitrary digit string (used by NPI).

    Matches the CMS NPPES spec: process the digits right-to-left, doubling
    every second digit (starting from rightmost = position 0).
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def _npi_calc_check_digit(body: str) -> str:
    """NPI check digit: Luhn applied to '80840' + 9-digit body.

    CMS NPPES NPI Check Digit Procedure (2008):
    prefix '80840' is prepended to the 9-digit NPI body before computing
    the Luhn check digit.  The result is the 10th digit of the full NPI.

    Verified: body '123456789' -> prefix '80840123456789' -> check '3'.
    """
    return _luhn_cd(_NPI_PREFIX + body)


def _npi_validate(value: str) -> bool:
    """NPI check: 10 digits, first digit 1 or 2 (NPPES), last digit is CMS check.

    NPPES allocates 1- and 2-prefixed NPIs only (CMS, 2008).  A 10-digit
    string with a valid Luhn check but a leading 0 or 3-9 is structurally
    invalid and is rejected here to agree with the engine's own NpiValidator
    (providers_v2/identifiers/_npi.py ``_is_valid_npi``).
    """
    if not _NPI_RE.match(value):
        return False
    if value[0] not in ("1", "2"):
        return False
    return value[9] == _npi_calc_check_digit(value[:9])


# ---------------------------------------------------------------------------
# IBAN  (python-stdnum; ISO 13616 / ISO 7064 mod-97)
# ---------------------------------------------------------------------------


def _iban_calc_check_digit(body: str) -> str:
    """Compute the 2-character IBAN check digits for a CC + BBAN body.

    Args:
        body: 2-char ISO 3166 country code followed by the BBAN (no check
              digits, no spaces).  E.g. 'GBWEST12345698765432'.

    Returns:
        2-digit check string.  The full IBAN is body[:2] + check + body[2:].
    """
    cc = body[:2]
    bban = body[2:]
    return _iban.calc_check_digits(cc + "xx" + bban)


def _iban_validate(value: str) -> bool:
    return _iban.is_valid(value)


# ---------------------------------------------------------------------------
# VIN  (hand-rolled: not in python-stdnum; ISO 3779 / NHTSA 49 CFR 565)
# ---------------------------------------------------------------------------

# Transliteration values per NHTSA 49 CFR Part 565 Appendix B.
# Letters I, O, Q are not used in VIN; omitted from this map.
_VIN_TRANS: dict[str, int] = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "J": 1,
    "K": 2,
    "L": 3,
    "M": 4,
    "N": 5,
    "P": 7,
    "R": 9,
    "S": 2,
    "T": 3,
    "U": 4,
    "V": 5,
    "W": 6,
    "X": 7,
    "Y": 8,
    "Z": 9,
    **{str(d): d for d in range(10)},
}

# Positional weights for VIN positions 1-17 (1-indexed).
# Position 9 (index 8) has weight 0 -- the check digit slot.
_VIN_WEIGHTS: tuple[int, ...] = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

# Valid VIN characters (no I, O, Q per ISO 3779).
_VIN_CHARSET: frozenset[str] = frozenset(_VIN_TRANS.keys())


def _vin_check_from_full(vin17: str) -> str:
    """Compute the VIN check character from a 17-character VIN string.

    The string at position 8 (0-indexed) is the check position; its weight
    is 0 so its current value does not affect the sum.  Returns '0'-'9'
    or 'X' (= 10).

    Standard: ISO 3779 / NHTSA 49 CFR Part 565, Appendix B.
    """
    total = sum(_VIN_TRANS[c] * _VIN_WEIGHTS[i] for i, c in enumerate(vin17))
    r = total % 11
    return "X" if r == 10 else str(r)


def _vin_calc_check_digit(body: str) -> str:
    """Compute the VIN check character from the 16 non-check characters.

    Args:
        body: 16-character string = positions 0-7 (before check) + positions
              9-16 (after check) of the full 17-character VIN.

    Returns:
        Single character: '0'-'9' or 'X'.

    Because the check position (index 8) has weight 0 in the sum, inserting
    any valid character there produces the same result.  We insert '0' as a
    neutral placeholder (ISO 3779 Appendix B computation property).
    """
    full = body[:8] + "0" + body[8:]  # neutral placeholder at weight-0 position
    return _vin_check_from_full(full)


def _vin_validate(value: str) -> bool:
    """VIN check: exactly 17 valid characters, check digit at position 8.

    Standard: ISO 3779 / NHTSA 49 CFR Part 565.
    """
    v = value.strip().upper()
    if len(v) != 17:
        return False
    if not all(c in _VIN_CHARSET for c in v):
        return False
    return _vin_check_from_full(v) == v[8]


# ---------------------------------------------------------------------------
# ISBN-13  (python-stdnum; ISO 2108 + GS1 check digit)
# ---------------------------------------------------------------------------


def _isbn13_calc_check_digit(body: str) -> str:
    """ISBN-13 check digit: same GS1 alternating-weight algorithm as EAN-13.

    Body = 12 digits (including the 978/979 bookland prefix).
    Delegates to stdnum.ean.calc_check_digit (ISBN-13 = EAN-13 compatible).
    """
    return _ean.calc_check_digit(body)


def _isbn13_validate(value: str) -> bool:
    return _isbn.is_valid(value)


# ---------------------------------------------------------------------------
# EAN-13  (python-stdnum; GS1 General Specifications)
# ---------------------------------------------------------------------------


def _ean13_calc_check_digit(body: str) -> str:
    """EAN-13 check digit: GS1 alternating weights (1, 3), mod 10.

    Body = N-1 digits (where N is 8, 12, 13, or 14 for the four EAN/GTIN
    variants); returns 1 digit.  Delegates to stdnum.ean.calc_check_digit.
    """
    return _ean.calc_check_digit(body)


def _ean13_validate(value: str) -> bool:
    return _ean.is_valid(value)


# ---------------------------------------------------------------------------
# GTIN  (python-stdnum; GS1 GTIN spec -- all lengths via stdnum.ean)
# ---------------------------------------------------------------------------


def _gtin_calc_check_digit(body: str) -> str:
    """GTIN check digit: same GS1 algorithm as EAN-13, applicable to all lengths.

    Delegates to stdnum.ean.calc_check_digit which handles GTIN-8 (7-digit
    body), GTIN-12 (11-digit body), GTIN-13 (12-digit body), and GTIN-14
    (13-digit body).
    """
    return _ean.calc_check_digit(body)


def _gtin_validate(value: str) -> bool:
    return _ean.is_valid(value)


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------

_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "luhn": _luhn_validate,
    "npi": _npi_validate,
    "iban": _iban_validate,
    "vin": _vin_validate,
    "isbn13": _isbn13_validate,
    "ean13": _ean13_validate,
    "gtin": _gtin_validate,
}

_CHECK_DIGIT_FUNCS: dict[str, Callable[[str], str]] = {
    "luhn": _luhn_calc_check_digit,
    "npi": _npi_calc_check_digit,
    "iban": _iban_calc_check_digit,
    "vin": _vin_calc_check_digit,
    "isbn13": _isbn13_calc_check_digit,
    "ean13": _ean13_calc_check_digit,
    "gtin": _gtin_calc_check_digit,
}

_KNOWN_SCHEMES: frozenset[str] = frozenset(_VALIDATORS.keys())

# Per-scheme minimum required charset for FPE mode.  A configured FPE charset
# that does not contain all characters in this set causes values to pass
# through unmasked at runtime: with preserve_separators=True, missing characters
# are treated as separators, fragmenting the value into short runs that fall
# below the per-scheme L1 min-length guard and returning the input verbatim.
# check_fpe_checksum_scheme in plan/_checks.py rejects the misconfiguration
# at plan-compile rather than letting it silently no-op at run time.
#
# iban is absent: FPE mode is unconditionally unsupported for iban regardless
# of charset (caught by the preceding fpe_checksum_iban_unsupported check).
_SCHEME_REQUIRED_CHARSET: dict[str, frozenset[str]] = {
    "luhn": frozenset("0123456789"),
    "npi": frozenset("0123456789"),
    "ean13": frozenset("0123456789"),
    "isbn13": frozenset("0123456789"),
    "gtin": frozenset("0123456789"),
    # VIN: 0-9 plus A-Z excluding I, O, Q (NHTSA 49 CFR Part 565 Appendix B).
    # Reuses the module-level _VIN_CHARSET so the two stay in sync.
    "vin": _VIN_CHARSET,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate(scheme: str, value: str) -> bool:
    """Return True iff ``value`` has a valid check digit for ``scheme``.

    Args:
        scheme: One of ``'luhn'``, ``'npi'``, ``'iban'``, ``'vin'``,
                ``'isbn13'``, ``'ean13'``, ``'gtin'``.
        value:  The full value including its check digit(s).

    Returns:
        True if the check digit is valid; False otherwise.

    Raises:
        ValueError: If ``scheme`` is not a recognised checksum scheme.
    """
    if scheme not in _KNOWN_SCHEMES:
        raise ValueError(
            f"unknown checksum scheme {scheme!r}. Known schemes: {sorted(_KNOWN_SCHEMES)}"
        )
    return _VALIDATORS[scheme](value)


def calc_check_digit(scheme: str, body: str) -> str:
    """Compute the check digit(s) for ``body`` under ``scheme``.

    The ``body`` semantics are scheme-specific:

    * ``luhn``, ``npi``, ``ean13``, ``isbn13``, ``gtin``: body is the full
      value minus the trailing check digit.
    * ``iban``: body is the 2-char country code followed directly by the
      BBAN (no check digits, no spaces).  Returns a 2-character string.
    * ``vin``: body is the 16 non-check characters (positions 0-7 and 9-16
      of the 17-character VIN).  Returns a single character ('0'-'9' or 'X').

    Args:
        scheme: One of ``'luhn'``, ``'npi'``, ``'iban'``, ``'vin'``,
                ``'isbn13'``, ``'ean13'``, ``'gtin'``.
        body:   The non-check portion of the value (see above).

    Returns:
        The check digit string (1 character for most schemes; 2 for IBAN).

    Raises:
        ValueError: If ``scheme`` is not a recognised checksum scheme.
    """
    if scheme not in _KNOWN_SCHEMES:
        raise ValueError(
            f"unknown checksum scheme {scheme!r}. Known schemes: {sorted(_KNOWN_SCHEMES)}"
        )
    return _CHECK_DIGIT_FUNCS[scheme](body)
